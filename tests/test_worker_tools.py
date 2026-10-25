"""Strict, credential-safe loading of a task's explicit MCP tools manifest."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import worker_tools as wt  # noqa: E402
import local_worker as lw  # noqa: E402

TOKEN = 'Bearer super-secret-token-8f2c'
OTHER = 'another-secret-value-42'


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.prog = self.executable('ctx-mcp')
        self.executable('git')
        self.env = {'PATH': str(self.bin), 'TASK_TOKEN': TOKEN, 'TASK_DIR': str(self.worktree)}

    def executable(self, name):
        path = self.bin / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
        return path

    def write(self, text):
        path = self.root / 'tools.json'
        path.write_text(text if isinstance(text, str) else json.dumps(text))
        return path

    def load(self, manifest, env=None, worktree=None):
        path = self.write(manifest)
        return wt.load_tool_config(path, self.env if env is None else env,
                                   self.worktree if worktree is None else worktree)

    def stdio(self, **spec):
        server = {'command': 'ctx-mcp', 'enabled_tools': ['read_file']}
        server.update(spec)
        return {'schema_version': 1, 'mcp_servers': {'ctx': server}}

    def pairs(self, result):
        argv = result['overrides']
        self.assertEqual(len(argv) % 2, 0)
        self.assertEqual(argv[0::2], ['-c'] * (len(argv) // 2))
        return dict(part.split('=', 1) for part in argv[1::2])

    def secret_free(self, result):
        blob = json.dumps(result['evidence'], sort_keys=True) + result['prompt'] + '\n'.join(result['overrides'])
        for secret in (TOKEN, OTHER):
            self.assertNotIn(secret, blob)


class EmptyManifestTests(ManifestTests):
    def test_none_path_returns_empty_structures(self):
        result = wt.load_tool_config(None, self.env, self.worktree)
        self.assertEqual(set(result), {'overrides', 'environment', 'secrets', 'evidence', 'prompt'})
        self.assertEqual(result['overrides'], [])
        self.assertEqual(result['environment'], {})
        self.assertEqual(result['secrets'], {})
        self.assertEqual(result['evidence'], {})
        self.assertEqual(result['prompt'], '')

    def test_minimal_manifest_has_no_servers_or_overrides(self):
        result = self.load({'schema_version': 1})
        self.assertEqual(result['overrides'], [])
        self.assertEqual(result['environment'], {})
        self.assertEqual(result['secrets'], {})
        self.assertEqual(result['evidence']['schema_version'], 1)
        self.assertEqual(result['evidence']['servers'], {})
        self.assertEqual(result['evidence']['required_commands'], {})
        self.secret_free(result)


class StdioTests(ManifestTests):
    def test_stdio_success_uses_defaults_and_resolved_command(self):
        result = self.load(self.stdio())
        pairs = self.pairs(result)
        self.assertEqual(pairs['mcp_servers.ctx.enabled'], 'true')
        self.assertEqual(pairs['mcp_servers.ctx.required'], 'true')
        self.assertEqual(pairs['mcp_servers.ctx.command'], json.dumps(str(self.prog)))
        self.assertEqual(pairs['mcp_servers.ctx.cwd'], json.dumps(str(self.worktree)))
        self.assertEqual(pairs['mcp_servers.ctx.startup_timeout_sec'], '10')
        self.assertEqual(pairs['mcp_servers.ctx.tool_timeout_sec'], '60')
        self.assertEqual(json.loads(pairs['mcp_servers.ctx.enabled_tools']), ['read_file'])
        self.assertNotIn('mcp_servers.ctx.args', pairs)
        server = result['evidence']['servers']['ctx']
        self.assertEqual(server['configured'], True)
        self.assertEqual(server['probed'], False)
        self.assertEqual(server['observed'], False)
        self.assertEqual(server['transport'], 'stdio')
        self.assertEqual(server['command'], str(self.prog))
        self.secret_free(result)

    def test_stdio_args_cwd_env_vars_and_timeouts(self):
        extra = self.bin.parent / 'extra'
        extra.mkdir()
        self.env['OTHER_TOKEN'] = OTHER
        result = self.load(self.stdio(args=['--profile', 'plain text "quoted"'],
                                      cwd=str(extra), env_vars=['OTHER_TOKEN'],
                                      startup_timeout_sec=1, tool_timeout_sec=300))
        pairs = self.pairs(result)
        self.assertEqual(json.loads(pairs['mcp_servers.ctx.args']), ['--profile', 'plain text "quoted"'])
        self.assertEqual(json.loads(pairs['mcp_servers.ctx.cwd']), str(extra))
        self.assertEqual(pairs['mcp_servers.ctx.startup_timeout_sec'], '1')
        self.assertEqual(pairs['mcp_servers.ctx.tool_timeout_sec'], '300')
        self.assertEqual(pairs['mcp_servers.ctx.env_vars'], json.dumps(['OTHER_TOKEN']))
        self.assertEqual(result['environment'], {'OTHER_TOKEN': OTHER})
        self.assertEqual(result['secrets'], {'OTHER_TOKEN': OTHER})
        self.assertEqual(result['evidence']['servers']['ctx']['env_var_names'], ['OTHER_TOKEN'])
        self.secret_free(result)

    def test_absent_executable_names_the_command(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            self.load(self.stdio(command='not-installed-here'), env={'PATH': str(self.bin)})
        self.assertIn('not-installed-here', str(caught.exception))

    def test_command_resolution_uses_supplied_path_only(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.stdio(command='ctx-mcp'), env={'PATH': str(self.worktree)})

    def test_missing_path_cannot_resolve_command_names(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            self.load(self.stdio(command='ctx-mcp'), env={'TASK_TOKEN': TOKEN})
        self.assertIn('PATH', str(caught.exception))

    def test_absolute_command_path_must_be_an_executable_file(self):
        plain = self.root / 'plain.txt'
        plain.write_text('no shebang\n')
        plain.chmod(0o644)
        for bad in (str(plain), str(self.bin), 'ctx-mcp/../ctx-mcp', 7):
            with self.subTest(bad=bad):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(command=bad))

    def test_executable_absolute_path_is_accepted(self):
        result = self.load(self.stdio(command=str(self.prog)))
        self.assertEqual(self.pairs(result)['mcp_servers.ctx.command'], json.dumps(str(self.prog)))

    def test_relative_path_entry_resolves_to_an_absolute_worktree_executable(self):
        program = self.worktree / 'rel-mcp'
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o755)
        result = self.load(self.stdio(command='rel-mcp'), env={'PATH': '.', 'TASK_DIR': str(self.worktree)})
        command = json.loads(self.pairs(result)['mcp_servers.ctx.command'])
        self.assertEqual(command, str(program))
        self.assertTrue(Path(command).is_absolute())
        self.assertEqual(result['evidence']['servers']['ctx']['command'], str(program))
        self.secret_free(result)

    def test_cwd_must_be_absolute_existing_directory(self):
        missing = self.root / 'absent'
        for bad in ('.', 'relative/dir', str(missing), str(plain_file := self.executable('ctx-mcp'))):
            with self.subTest(bad=bad):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(cwd=bad))

    def test_required_env_var_must_be_present_and_nonempty(self):
        for env in ({'PATH': str(self.bin)}, {'PATH': str(self.bin), 'TASK_TOKEN': '   '}):
            with self.subTest(missing=env):
                with self.assertRaises(wt.ToolConfigError) as caught:
                    self.load(self.stdio(env_vars=['TASK_TOKEN']), env=env)
                self.assertIn('TASK_TOKEN', str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))


class HttpTests(ManifestTests):
    def http(self, url='https://mcp.example.invalid/sse', **spec):
        server = {'url': url, 'enabled_tools': ['search']}
        server.update(spec)
        return {'schema_version': 1, 'mcp_servers': {'api': server}}

    def test_https_success_reports_host_only(self):
        result = self.load(self.http(bearer_token_env_var='TASK_TOKEN'))
        pairs = self.pairs(result)
        self.assertEqual(pairs['mcp_servers.api.url'], json.dumps('https://mcp.example.invalid/sse'))
        self.assertEqual(pairs['mcp_servers.api.bearer_token_env_var'], '"TASK_TOKEN"')
        self.assertEqual(pairs['mcp_servers.api.enabled'], 'true')
        self.assertEqual(pairs['mcp_servers.api.required'], 'true')
        self.assertEqual(pairs['mcp_servers.api.startup_timeout_sec'], '10')
        self.assertEqual(pairs['mcp_servers.api.tool_timeout_sec'], '60')
        server = result['evidence']['servers']['api']
        self.assertEqual(server['transport'], 'http')
        self.assertEqual(server['host'], 'mcp.example.invalid')
        self.assertEqual(server['bearer_token_env_var'], 'TASK_TOKEN')
        self.assertNotIn('url', server)
        self.assertNotIn('command', server)
        self.assertEqual(result['environment'], {'TASK_TOKEN': TOKEN})
        self.assertEqual(result['secrets'], {'TASK_TOKEN': TOKEN})
        self.secret_free(result)

    def test_loopback_http_is_allowed(self):
        for url in ('http://localhost:8080/mcp', 'http://127.0.0.1/mcp', 'http://[::1]:9/mcp'):
            with self.subTest(url=url):
                result = self.load(self.http(url))
                self.assertIn('mcp_servers.api.url', self.pairs(result))

    def test_public_http_and_credential_embedding_are_refused(self):
        bad = ['http://mcp.example.invalid/mcp', 'https://user:pw@mcp.example.invalid/mcp',
               'https://mcp.example.invalid/mcp?code=' + TOKEN, 'https://mcp.example.invalid/mcp#frag',
               'ftp://mcp.example.invalid/mcp', 'https:///mcp', 'https:///mcp', TOKEN]
        for url in bad:
            with self.subTest(url=url if url == TOKEN else url[:20]):
                with self.assertRaises(wt.ToolConfigError) as caught:
                    self.load(self.http(url))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_rejects_stdio_only_keys_and_stdio_rejects_http_only_keys(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.http(args=['--x']))
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.http(bearer_token_env_var='TASK_TOKEN', env_vars=['TASK_TOKEN']))
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.stdio(bearer_token_env_var='TASK_TOKEN'))

    def test_transports_cannot_be_combined_or_omitted(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.stdio(url='https://mcp.example.invalid/mcp'))
        with self.assertRaises(wt.ToolConfigError):
            self.load({'schema_version': 1, 'mcp_servers': {'ctx': {'enabled_tools': ['t']}}})


class SchemaTests(ManifestTests):
    def test_unknown_top_level_keys_and_bad_containers(self):
        for manifest in ({'schema_version': 1, 'mcp_server': {}},
                         {'schema_version': 1, 'required_commands': 'git'},
                         {'schema_version': 1, 'mcp_servers': []},
                         {'schema_version': 1, 'mcp_servers': {'ctx': []}},
                         {'schema_version': 1, 'required_commands': ['']},
                         {'schema_version': 1, 'required_commands': [7]}):
            with self.subTest(keys=sorted(manifest)):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(manifest)

    def test_schema_version_must_be_exactly_integer_one(self):
        for version in (2, '1', True, 1.0, None):
            with self.subTest(version=version):
                with self.assertRaises(wt.ToolConfigError):
                    self.load({'schema_version': version})
        with self.assertRaises(wt.ToolConfigError):
            self.load({'required_commands': []})

    def test_unknown_server_keys_and_literal_env_values(self):
        for spec in ({'transport': 'stdio'}, {'env': {'TASK_TOKEN': TOKEN}},
                     {'headers': {'Authorization': TOKEN}}, {'disabled': False}):
            with self.subTest(keys=sorted(spec)):
                with self.assertRaises(wt.ToolConfigError) as caught:
                    self.load(self.stdio(**spec))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_enabled_tools_required_nonempty_and_unique(self):
        for tools in ('read_file', [], ['read_file', 'read_file'], [7], None):
            with self.subTest(tools=tools):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(enabled_tools=tools))
        with self.assertRaises(wt.ToolConfigError):
            self.load({'schema_version': 1, 'mcp_servers': {'ctx': {'command': 'ctx-mcp'}}})

    def test_timeouts_are_bounded_integers(self):
        for value in (0, 121, True, 10.5, '10'):
            with self.subTest(field='startup_timeout_sec', value=value):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(startup_timeout_sec=value))
        for value in (0, 301, False, 60.0):
            with self.subTest(field='tool_timeout_sec', value=value):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(tool_timeout_sec=value))

    def test_nonfinite_numbers_are_refused(self):
        path = self.write('{"schema_version": 1, "mcp_servers": {"ctx": {"command": "ctx-mcp", '
                          '"enabled_tools": ["t"], "startup_timeout_sec": 1e400}}}')
        with self.assertRaises(wt.ToolConfigError):
            wt.load_tool_config(path, self.env, self.worktree)

    def test_args_must_be_a_list_of_strings(self):
        for args in ('--profile', ['--profile', 7], {'a': 'b'}):
            with self.subTest(args=args):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(args=args))

    def test_env_vars_must_be_a_list_and_bearer_a_bare_name(self):
        for value in ('TASK_TOKEN', 'TASK_TOKEN OTHER_TOKEN', {'TASK_TOKEN': 'x'}, 7, ['TASK_TOKEN', 7]):
            with self.subTest(env_vars=value):
                with self.assertRaises(wt.ToolConfigError) as caught:
                    self.load(self.stdio(env_vars=value))
                self.assertNotIn(TOKEN, str(caught.exception))
        for value in (['TASK_TOKEN'], ['TASK_TOKEN', 'OTHER_TOKEN'], {'name': 'TASK_TOKEN'}, 7):
            with self.subTest(bearer_token_env_var=value):
                with self.assertRaises(wt.ToolConfigError):
                    self.load({'schema_version': 1, 'mcp_servers': {'api': {
                        'url': 'https://mcp.example.invalid/mcp', 'enabled_tools': ['t'],
                        'bearer_token_env_var': value}}})

    def test_server_names_must_be_safe_keys(self):
        for name in ('ctx server', '1ctx', '-ctx', 'mcp.servers', '', 'ctx/../model', 'ünïcode'):
            with self.subTest(name=name):
                with self.assertRaises(wt.ToolConfigError):
                    self.load({'schema_version': 1, 'mcp_servers': {name: {'command': 'ctx-mcp',
                                                                           'enabled_tools': ['t']}}})

    def test_duplicate_json_keys_are_refused(self):
        for text in ('{"schema_version": 1, "schema_version": 1}',
                     '{"schema_version": 1, "mcp_servers": {"ctx": {"command": "ctx-mcp", "command": "git", '
                     '"enabled_tools": ["t"]}}}'):
            with self.subTest(text=text[:24]):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(text)

    def test_malformed_json_is_refused(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load('{"schema_version": 1,')

    def test_unreadable_manifest_reports_the_path(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            wt.load_tool_config(self.root / 'absent.json', self.env, self.worktree)
        self.assertIn('absent.json', str(caught.exception))


class EnvironmentGuardTests(ManifestTests):
    RESERVED = ['PATH', 'HOME', 'PYTHONPATH', 'PYTHONHOME', 'CODEX_HOME', 'CODEX_MODEL',
                'LOCAL_WORKER_MODEL', 'LOCAL_QWEN_TOKEN', 'OMLX_MODEL', 'OPENAI_API_KEY',
                'LOCAL_OMLX_API_KEY',
                'ANTHROPIC_API_KEY', 'http_proxy', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
                'LD_PRELOAD', 'DYLD_LIBRARY_PATH']

    def test_routing_and_system_environment_names_are_refused(self):
        self.env.update({name: TOKEN for name in self.RESERVED if name != 'PATH'})
        for name in self.RESERVED:
            with self.subTest(name=name):
                with self.assertRaises(wt.ToolConfigError) as caught:
                    self.load(self.stdio(env_vars=[name]))
                self.assertIn(name.upper(), str(caught.exception).upper())
                self.assertNotIn(TOKEN, str(caught.exception))
                with self.assertRaises(wt.ToolConfigError):
                    self.load({'schema_version': 1, 'mcp_servers': {'api': {
                        'url': 'https://mcp.example.invalid/mcp', 'enabled_tools': ['t'],
                        'bearer_token_env_var': name}}})

    def test_environment_names_must_be_bare_identifiers(self):
        for name in ('TASK-TOKEN', '1TASK', 'TASK TOKEN', 'TASK_TOKEN=x', '$TASK_TOKEN', ''):
            with self.subTest(name=name):
                with self.assertRaises(wt.ToolConfigError):
                    self.load(self.stdio(env_vars=[name]))

    def test_duplicate_environment_names_are_refused(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.stdio(env_vars=['TASK_TOKEN', 'TASK_TOKEN']))

    def test_nonstring_environment_values_are_refused(self):
        with self.assertRaises(wt.ToolConfigError):
            self.load(self.stdio(env_vars=['TASK_TOKEN']), env={'PATH': str(self.bin), 'TASK_TOKEN': 7})

    def test_non_mapping_environment_is_refused(self):
        with self.assertRaises(wt.ToolConfigError):
            wt.load_tool_config(self.write(self.stdio()), 'PATH=' + str(self.bin), self.worktree)


class RoutingPreservationTests(ManifestTests):
    def test_overrides_only_touch_mcp_servers(self):
        manifest = {'schema_version': 1, 'required_commands': ['git'],
                    'mcp_servers': {'ctx': {'command': 'ctx-mcp', 'enabled_tools': ['read_file']},
                                    'api': {'url': 'https://mcp.example.invalid/mcp',
                                            'enabled_tools': ['search'], 'bearer_token_env_var': 'TASK_TOKEN'}}}
        result = self.load(manifest)
        pairs = self.pairs(result)
        self.assertTrue(pairs, 'expected at least one override')
        for key in pairs:
            self.assertTrue(key.startswith('mcp_servers.'), key)
            self.assertNotIn('model', key)
            self.assertNotIn('provider', key)
            self.assertNotIn('sandbox', key)
        self.assertEqual(sorted(p for p in pairs if p.endswith('.enabled')),
                         ['mcp_servers.api.enabled', 'mcp_servers.ctx.enabled'])

    def test_alias_style_names_and_quoting_round_trip(self):
        tools = ['read-file', 'write_file.v2', 'say "hi"', 'back\\slash', 'tab\tchar', 'emoji \u00e9']
        result = self.load({'schema_version': 1,
                            'mcp_servers': {'my-server_v2': {'command': 'ctx-mcp', 'enabled_tools': tools}}})
        pairs = self.pairs(result)
        self.assertEqual(json.loads(pairs['mcp_servers.my-server_v2.enabled_tools']), tools)
        self.assertEqual(wt.toml_key('my-server_v2'), 'my-server_v2')
        self.assertEqual(json.loads(wt.toml_key('weird key')), 'weird key')
        for value in tools + ['https://x.invalid/a?b=c', '', '\u00e9']:
            self.assertEqual(json.loads(wt.toml_value(value)), value)
        self.assertEqual(wt.toml_value(True), 'true')
        self.assertEqual(wt.toml_value(60), '60')
        self.assertEqual(json.loads(wt.toml_value(['a', 'b'])), ['a', 'b'])

    def test_read_tier_wraps_stdio_servers_and_marks_http_trust(self):
        loaded = {"overrides": ["-c", 'mcp_servers.ctx.command="/usr/bin/python3"',
                                "-c", 'mcp_servers.ctx.args=["-m","fixture"]',
                                "-c", 'mcp_servers.remote.url="https://mcp.example"'],
                  "environment": {}, "secrets": {},
                  "evidence": {"servers": {
                      "ctx": {"transport": "stdio"},
                      "remote": {"transport": "http"}}}}
        result = wt.wrap_stdio_commands(loaded, Path("/tmp/readonly.sb"))
        overrides = lw.config_overrides_map(["codex", "exec", *result["overrides"]])
        self.assertEqual(json.loads(overrides["mcp_servers.ctx.command"]), "/usr/bin/sandbox-exec")
        self.assertEqual(json.loads(overrides["mcp_servers.ctx.args"]),
                         ["-f", "/tmp/readonly.sb", "/usr/bin/python3", "-m", "fixture"])
        self.assertIn("read-only", result["evidence"]["servers"]["ctx"]["process_wrapper"])
        self.assertIn("explicitly trusted by the task manifest",
                      result["evidence"]["servers"]["remote"]["process_wrapper"])
        self.assertEqual(loaded["overrides"][1], 'mcp_servers.ctx.command="/usr/bin/python3"')


class CredentialLeakTests(ManifestTests):
    def two_servers(self, args):
        return {'schema_version': 1,
                'mcp_servers': {'ctx': {'command': 'ctx-mcp', 'args': args,
                                        'enabled_tools': ['read_file']},
                                'api': {'url': 'https://mcp.example.invalid/sse',
                                        'enabled_tools': ['search'],
                                        'bearer_token_env_var': 'TASK_TOKEN'}}}

    def test_credential_valued_args_are_refused_before_rendering(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            self.load(self.stdio(args=[TOKEN], env_vars=['TASK_TOKEN']))
        self.assertIn('args', str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_credential_from_one_server_refuses_valued_args_in_another(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            self.load(self.two_servers(['--token', TOKEN]))
        self.assertIn('args', str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_harmless_args_survive_credentials_from_another_server(self):
        result = self.load(self.two_servers(['--format', 'json']))
        self.assertEqual(result['secrets'], {'TASK_TOKEN': TOKEN})
        self.assertEqual(result['environment'], {'TASK_TOKEN': TOKEN})
        self.assertEqual(json.loads(self.pairs(result)['mcp_servers.ctx.args']), ['--format', 'json'])
        self.assertEqual(json.loads(self.pairs(result)['mcp_servers.api.bearer_token_env_var']), 'TASK_TOKEN')
        self.secret_free(result)


class EvidenceTests(ManifestTests):
    def combined(self):
        return self.load({'schema_version': 1, 'required_commands': ['git', str(self.prog)],
                          'mcp_servers': {
                              'ctx': {'command': 'ctx-mcp', 'args': ['--format', 'json'],
                                      'enabled_tools': ['read_file'], 'env_vars': ['TASK_TOKEN']},
                              'api': {'url': 'https://mcp.example.invalid/sse',
                                      'enabled_tools': ['search'], 'bearer_token_env_var': 'TASK_TOKEN'}}})

    def test_secrets_never_reach_overrides_evidence_or_prompt(self):
        result = self.combined()
        self.assertEqual(result['secrets'], {'TASK_TOKEN': TOKEN})
        self.assertEqual(result['environment'], {'TASK_TOKEN': TOKEN})
        self.secret_free(result)

    def test_evidence_is_json_safe_and_records_digest_and_paths(self):
        result = self.combined()
        evidence = result['evidence']
        json.dumps(evidence)
        path = self.root / 'tools.json'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stale = self.write({'schema_version': 1}).read_bytes()
        self.assertNotEqual(evidence['manifest_sha256'], hashlib.sha256(stale).hexdigest())
        self.assertEqual(evidence['manifest_sha256'], digest)
        self.assertEqual(len(evidence['manifest_sha256']), 64)
        self.assertEqual(evidence['schema_version'], 1)
        self.assertEqual(evidence['required_commands'], {'git': str(self.bin / 'git'),
                                                          str(self.prog): str(self.prog)})
        self.assertEqual(sorted(evidence['servers']), ['api', 'ctx'])

    def test_evidence_omits_raw_args_and_literal_values(self):
        evidence = json.dumps(self.combined()['evidence'], sort_keys=True)
        self.assertNotIn('args', evidence)
        self.assertNotIn('--format', evidence)

    def test_required_commands_fail_with_names_and_no_values(self):
        with self.assertRaises(wt.ToolConfigError) as caught:
            self.load({'schema_version': 1, 'required_commands': ['absent-tool'],
                       'mcp_servers': {'ctx': {'command': 'ctx-mcp', 'enabled_tools': ['t']}}})
        self.assertIn('absent-tool', str(caught.exception))

    def test_prompt_lists_servers_tools_and_commands_only(self):
        prompt = self.combined()['prompt']
        self.assertIn('ctx', prompt)
        self.assertIn('api', prompt)
        self.assertIn('read_file', prompt)
        self.assertIn('search', prompt)
        self.assertIn('git', prompt)
        self.assertNotIn('--format', prompt)
        self.secret_free(self.combined())

    def test_prompt_is_compact(self):
        prompt = self.combined()['prompt']
        self.assertLess(len(prompt.splitlines()), 12)


if __name__ == '__main__':
    unittest.main()
