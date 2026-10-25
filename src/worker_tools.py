"""Explicit, credential-safe MCP tool configuration for a single worker task.

The manifest is an approved allow-list, never a place for live values: servers
name the environment variables they need, and this loader turns that into Codex
``-c`` overrides plus the small child environment and evidence the runner needs.
Nothing here probes a network, starts a server, or copies a credential into a
value that could reach argv, evidence, or the prompt.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit
import permissions

SCHEMA_VERSION = 1
MAX_STARTUP_TIMEOUT_SEC = 120
MAX_TOOL_TIMEOUT_SEC = 300
DEFAULT_STARTUP_TIMEOUT_SEC = 10
DEFAULT_TOOL_TIMEOUT_SEC = 60

MANIFEST_KEYS = frozenset({'schema_version', 'required_commands', 'mcp_servers'})
COMMON_SERVER_KEYS = frozenset({'enabled_tools', 'startup_timeout_sec', 'tool_timeout_sec'})
STDIO_KEYS = frozenset({'command', 'args', 'cwd', 'env_vars'}) | COMMON_SERVER_KEYS
HTTP_KEYS = frozenset({'url', 'bearer_token_env_var'}) | COMMON_SERVER_KEYS
LITERAL_VALUE_KEYS = frozenset({'env', 'headers', 'http_headers', 'env_values', 'bearer_token'})
TOOL_ITEM_TYPE = 'mcp_tool_call'
TOOL_LIFECYCLE = frozenset({'item.started', 'item.completed'})
MIN_SECRET_LENGTH = 4
REDACTED = '[REDACTED]'

SERVER_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]*')
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

RESERVED_ENV_PREFIXES = ('CODEX', 'LOCAL_WORKER', 'LOCAL_QWEN', 'LOCAL_OMLX', 'OMLX', 'OPENAI', 'ANTHROPIC', 'LD_', 'DYLD_')
RESERVED_ENV_NAMES = frozenset({'PATH', 'HOME', 'PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'HTTPS_PROXY',
                               'ALL_PROXY', 'NO_PROXY', 'FTP_PROXY', 'WSS_PROXY', 'GIT_SSH_COMMAND',
                               'GIT_CONFIG_GLOBAL', 'SSH_AUTH_SOCK'})

__all__ = ['ToolConfigError', 'SCHEMA_VERSION', 'load_tool_config', 'toml_key', 'toml_value',
           'redaction_secrets', 'selected_env_names', 'observed_tool_calls', 'mark_observations']


class ToolConfigError(ValueError):
    """The tools manifest, or the environment it names, is not safe to run."""


def toml_key(name):
    """Return a TOML key for *name*, quoting it when it is not a bare key."""
    if isinstance(name, str) and SERVER_NAME.fullmatch(name):
        return name
    return json.dumps(name, ensure_ascii=False)


def toml_value(value):
    """Render *value* as a TOML literal; JSON string escaping is a TOML subset."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    raise ToolConfigError('override values must be booleans, integers, strings, or lists of strings')


def is_reserved_env_name(name):
    """True when *name* could reroute the worker or its loader instead of the task."""
    upper = name.upper()
    return upper in RESERVED_ENV_NAMES or any(upper.startswith(prefix) for prefix in RESERVED_ENV_PREFIXES)


def _manifest(path):
    """Parse *path* for redaction only: a manifest that cannot be read yields nothing."""
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _named_credentials(manifest):
    """Variable names the manifest explicitly asks the caller to supply, never their values."""
    names = []
    servers = manifest.get('mcp_servers')
    if not isinstance(servers, dict):
        return names
    for spec in servers.values():
        if not isinstance(spec, dict):
            continue
        env_vars = spec.get('env_vars')
        if isinstance(env_vars, list):
            names.extend(item for item in env_vars if isinstance(item, str))
        token = spec.get('bearer_token_env_var')
        if isinstance(token, str):
            names.append(token)
    return names


def selected_env_names(path):
    """Names a manifest wants from the caller environment; never raises on a broken manifest."""
    manifest = _manifest(path)
    if manifest is None:
        return []
    return sorted(set(_named_credentials(manifest)))


def redaction_secrets(path, env):
    """Selected credential *values*, collectable before validation so even errors stay safe.

    Only a str value under a str name is ever collected: an untrusted manifest entry is never
    stringified into a redaction candidate, and a name that is absent contributes nothing.
    """
    names = selected_env_names(path)
    if not names or not hasattr(env, 'get'):
        return []
    values = []
    for name in names:
        try:
            value = env.get(name)
        except Exception:  # noqa: BLE001 - a hostile mapping must not break redaction
            continue
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH and value not in values:
            values.append(value)
    return values


def _redact(text, secrets):
    for value in secrets:
        text = text.replace(value, REDACTED)
    return text


def load_tool_config(path, env, worktree):
    """Validate a tools manifest; every refusal is redacted before it can leave the loader."""
    try:
        _reject_short_credentials(path, env)
        return _load(path, env, worktree)
    except ToolConfigError as error:
        safe = _redact(str(error), redaction_secrets(path, env if hasattr(env, 'get') else None))
        raise ToolConfigError(safe) from None


def _reject_short_credentials(path, env):
    """Refuse a selected credential too short to redact, before anything is resolved.

    A value under ``MIN_SECRET_LENGTH`` is never collected for redaction, so any later message
    could echo it. Refusing it up front keeps such a credential out of every path, and the
    refusal is generic because naming the value is exactly what must not happen.
    """
    if not hasattr(env, 'get'):
        return
    for name in selected_env_names(path):
        try:
            value = env.get(name)
        except Exception:  # noqa: BLE001 - a hostile mapping must not break the guard
            continue
        if isinstance(value, str) and value.strip() and len(value) < MIN_SECRET_LENGTH:
            raise ToolConfigError('a credential environment variable named by the tools manifest '
                                  'is set to a value too short to redact; it must be at least %d '
                                  'characters and its value is never shown' % MIN_SECRET_LENGTH)


def _load(path, env, worktree):
    """Validate a tools manifest and return overrides, environment, secrets, evidence, prompt."""
    caller_env = _env_mapping(env)
    if path is None:
        return {'overrides': [], 'environment': {}, 'secrets': {}, 'evidence': {}, 'prompt': ''}
    root = _worktree(worktree)
    raw = _read(path)
    manifest = _parse(raw)
    if 'schema_version' not in manifest:
        raise ToolConfigError('tools manifest must declare schema_version 1')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != SCHEMA_VERSION:
        raise ToolConfigError('tools manifest schema_version must be the integer 1')
    unknown = sorted(str(key) for key in manifest if key not in MANIFEST_KEYS)
    if unknown:
        raise ToolConfigError('tools manifest has unsupported keys: ' + ', '.join(unknown))

    required = _required_commands(manifest.get('required_commands', []), caller_env, root)
    servers = manifest.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ToolConfigError('mcp_servers must be a map of server names to configuration')

    specs = []
    environment = {}
    secrets = {}
    for name in sorted(servers):
        spec = servers[name]
        if not isinstance(name, str) or not SERVER_NAME.fullmatch(name):
            raise ToolConfigError('mcp server names must match [A-Za-z][A-Za-z0-9_-]*')
        if not isinstance(spec, dict):
            raise ToolConfigError("mcp server '%s' configuration must be a map" % name)
        literals = sorted(str(key) for key in spec if key in LITERAL_VALUE_KEYS)
        if literals:
            raise ToolConfigError("mcp server '%s' may not carry literal env or header values (%s); "
                                  'name the variables instead' % (name, ', '.join(literals)))
        parts = _server(name, spec, caller_env, root)
        _collect(_credential_names(parts), caller_env, name, environment, secrets)
        specs.append(parts)

    _reject_secret_values(required, specs, secrets)

    pairs = []
    evidence_servers = {}
    for parts in specs:
        pairs.extend(_overrides(parts))
        evidence_servers[parts['name']] = _meta(parts)

    overrides = []
    for key, value in pairs:
        _check_override_key(key)
        overrides.extend(['-c', '%s=%s' % (key, value)])
    evidence = {'schema_version': SCHEMA_VERSION,
                'manifest_sha256': hashlib.sha256(raw).hexdigest(),
                'required_commands': required,
                'servers': evidence_servers}
    prompt = _prompt(evidence_servers, required)
    json.dumps(evidence, sort_keys=True)
    _assert_secret_free(overrides, evidence, prompt, secrets)
    return {'overrides': overrides, 'environment': environment, 'secrets': secrets,
            'evidence': evidence, 'prompt': prompt}


def _reject_secret_values(required, specs, secrets):
    """Refuse any public string that would carry a selected credential value into argv or evidence."""
    if not secrets:
        return
    for entry, resolved in sorted(required.items()):
        if _contains_secret(entry, secrets) or _contains_secret(resolved, secrets):
            raise ToolConfigError('required_commands must not contain a credential value; name an '
                                  'environment variable instead and keep the value out of the manifest')
    for parts in specs:
        fields = (('command', [parts['command']]), ('args', parts['args']), ('cwd', [parts['cwd']]),
                  ('enabled_tools', parts['tools']), ('env_vars', parts['env_names']),
                  ('url', [parts['url']]), ('bearer_token_env_var', [parts['token_name']]))
        for field, values in fields:
            if any(_contains_secret(value, secrets) for value in values):
                raise ToolConfigError("mcp server '%s' %s must not contain a credential value; name the "
                                      'environment variable instead and keep the value out of the manifest'
                                      % (parts['name'], field))


def _contains_secret(value, secrets):
    return isinstance(value, str) and any(candidate in value for candidate in secrets.values())


def _assert_secret_free(overrides, evidence, prompt, secrets):
    """Final guard: nothing that reaches argv, evidence, or the prompt may hold a credential value."""
    if not secrets:
        return
    blob = '\n'.join(overrides) + json.dumps(evidence, sort_keys=True) + prompt
    for value in secrets.values():
        if value in blob:
            raise ToolConfigError('tools configuration would expose a credential value in public output; '
                                  'name the environment variable instead')


def _server(name, spec, caller_env, root):
    """Validate one server and return its parts; nothing renders until every credential is known."""
    if 'command' in spec and 'url' in spec:
        raise ToolConfigError("mcp server '%s' must choose one transport: stdio command or http url" % name)
    if 'command' in spec:
        allowed, transport = STDIO_KEYS, 'stdio'
    elif 'url' in spec:
        allowed, transport = HTTP_KEYS, 'http'
    else:
        raise ToolConfigError("mcp server '%s' must define either a stdio command or an http url" % name)
    unknown = sorted(str(key) for key in spec if key not in allowed)
    if unknown:
        raise ToolConfigError("mcp server '%s' has unsupported keys: %s" % (name, ', '.join(unknown)))

    tools = _tool_list(spec, name)
    startup = _timeout(spec, 'startup_timeout_sec', 1, MAX_STARTUP_TIMEOUT_SEC, DEFAULT_STARTUP_TIMEOUT_SEC, name)
    tool_timeout = _timeout(spec, 'tool_timeout_sec', 1, MAX_TOOL_TIMEOUT_SEC, DEFAULT_TOOL_TIMEOUT_SEC, name)
    args = []
    cwd = str(root)
    env_names = []
    token_name = None
    scheme = host = None
    command = None
    url = None
    if transport == 'stdio':
        args = _args(spec, name)
        cwd = _cwd(spec, root, name)
        if 'env_vars' in spec:
            env_names = _env_names(spec['env_vars'], name, 'env_vars')
        command = _resolve(spec['command'], caller_env, "mcp server '%s' command" % name, root)
    else:
        url = spec['url']
        scheme, host = _url(url, name)
        if 'bearer_token_env_var' in spec:
            token_name = _env_name(spec['bearer_token_env_var'], name, 'bearer_token_env_var')
    return {'name': name, 'transport': transport, 'tools': tools, 'args': args, 'cwd': cwd,
            'env_names': env_names, 'token_name': token_name, 'url': url, 'command': command,
            'scheme': scheme, 'host': host, 'startup': startup, 'tool_timeout': tool_timeout}


def _credential_names(parts):
    names = list(parts['env_names'])
    if parts['token_name']:
        names.append(parts['token_name'])
    return names


def _overrides(parts):
    """Render validated *parts* as Codex overrides; only ever called after the credential scan."""
    key = 'mcp_servers.%s' % toml_key(parts['name'])
    pairs = [('%s.enabled' % key, True), ('%s.required' % key, True)]
    if parts['transport'] == 'stdio':
        pairs.append(('%s.command' % key, parts['command']))
        if parts['args']:
            pairs.append(('%s.args' % key, list(parts['args'])))
        pairs.append(('%s.cwd' % key, parts['cwd']))
        if parts['env_names']:
            pairs.append(('%s.env_vars' % key, list(parts['env_names'])))
    else:
        pairs.append(('%s.url' % key, parts['url']))
        if parts['token_name']:
            pairs.append(('%s.bearer_token_env_var' % key, parts['token_name']))
    pairs.append(('%s.enabled_tools' % key, list(parts['tools'])))
    pairs.append(('%s.startup_timeout_sec' % key, parts['startup']))
    pairs.append(('%s.tool_timeout_sec' % key, parts['tool_timeout']))
    return [(item[0], toml_value(item[1])) for item in pairs]


def wrap_stdio_commands(loaded, profile_path):
    """Return a loaded tools config whose stdio servers run in a read-only Seatbelt profile."""
    if not isinstance(loaded, dict):
        raise ToolConfigError('loaded tools configuration must be a map')
    copied = dict(loaded)
    pairs = []
    raw = list(loaded.get('overrides') or [])
    index = 0
    while index < len(raw):
        if raw[index] == '-c' and index + 1 < len(raw) and '=' in str(raw[index + 1]):
            key, _, value = str(raw[index + 1]).partition('=')
            pairs.append((key, value))
            index += 2
        else:
            index += 1
    evidence = json.loads(json.dumps(loaded.get('evidence') or {}))
    servers = evidence.get('servers') if isinstance(evidence, dict) else None
    rewritten = dict(pairs)
    for name, meta in (servers or {}).items():
        if not isinstance(meta, dict) or meta.get('transport') != 'stdio':
            if isinstance(meta, dict) and meta.get('transport') == 'http':
                meta['process_wrapper'] = 'not applicable; remote HTTP transport is explicitly trusted by the task manifest'
            continue
        prefix = 'mcp_servers.%s' % toml_key(name)
        command_key, args_key = prefix + '.command', prefix + '.args'
        try:
            command = json.loads(rewritten[command_key])
            args = json.loads(rewritten.get(args_key, '[]'))
        except (KeyError, ValueError, TypeError):
            raise ToolConfigError("stdio server '%s' could not be safely wrapped" % name) from None
        if not isinstance(command, str) or not command or not isinstance(args, list) \
                or any(not isinstance(item, str) for item in args):
            raise ToolConfigError("stdio server '%s' has an invalid command for wrapping" % name)
        rewritten[command_key] = toml_value(permissions.SANDBOX_EXEC)
        rewritten[args_key] = toml_value(['-f', str(profile_path), command, *args])
        meta['process_wrapper'] = 'macOS Seatbelt read-only MCP profile'
    copied['overrides'] = [item for key, value in rewritten.items()
                           for item in ('-c', key + '=' + value)]
    copied['evidence'] = evidence
    return copied


def _meta(parts):
    """Return the secret-free evidence record for one validated server."""
    meta = {'configured': True, 'probed': False, 'observed': False, 'transport': parts['transport'],
            'enabled_tools': list(parts['tools']), 'startup_timeout_sec': parts['startup'],
            'tool_timeout_sec': parts['tool_timeout']}
    if parts['transport'] == 'stdio':
        meta['command'] = parts['command']
        if parts['env_names']:
            meta['env_var_names'] = list(parts['env_names'])
    else:
        meta['scheme'] = parts['scheme']
        meta['host'] = parts['host']
        if parts['token_name']:
            meta['bearer_token_env_var'] = parts['token_name']
    return meta


def _args(spec, name):
    if 'args' not in spec:
        return []
    args = spec['args']
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ToolConfigError("mcp server '%s' args must be a list of strings" % name)
    return args


def _collect(names, caller_env, server, environment, secrets):
    for var in names:
        value = caller_env.get(var)
        if not isinstance(value, str) or not value.strip():
            raise ToolConfigError("environment variable %s required by mcp server '%s' is not set to a "
                                  'non-empty value; export it in the caller environment' % (var, server))
        environment[var] = value
        secrets[var] = value


def _tool_list(spec, name):
    if 'enabled_tools' not in spec:
        raise ToolConfigError("mcp server '%s' must list enabled_tools explicitly" % name)
    tools = spec['enabled_tools']
    if not isinstance(tools, list) or not tools or any(not isinstance(item, str) or not item.strip()
                                                       for item in tools):
        raise ToolConfigError("mcp server '%s' enabled_tools must be a non-empty list of tool names" % name)
    if len(set(tools)) != len(tools):
        raise ToolConfigError("mcp server '%s' enabled_tools must not repeat a tool name" % name)
    return tools


def _timeout(spec, field, low, high, default, name):
    if field not in spec:
        return default
    value = spec[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or type(value) is not int or not low <= value <= high:
        raise ToolConfigError("mcp server '%s' %s must be an integer between %d and %d" % (name, field, low, high))
    return value


def _env_names(value, name, field):
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not ENV_NAME.fullmatch(item)
                                                       for item in value):
        raise ToolConfigError("mcp server '%s' %s must be a list of bare environment variable names" % (name, field))
    if len(set(value)) != len(value):
        raise ToolConfigError("mcp server '%s' %s must not repeat a variable name" % (name, field))
    reserved = [item for item in value if is_reserved_env_name(item)]
    if reserved:
        raise ToolConfigError("mcp server '%s' %s may not pass routing or system variables: %s"
                              % (name, field, ', '.join(reserved)))
    return list(value)


def _env_name(value, name, field):
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise ToolConfigError("mcp server '%s' %s must be a bare environment variable name" % (name, field))
    if is_reserved_env_name(value):
        raise ToolConfigError("mcp server '%s' %s may not pass routing or system variables: %s"
                              % (name, field, value))
    return value


def _cwd(spec, root, name):
    if 'cwd' not in spec:
        return str(root)
    value = spec['cwd']
    if not isinstance(value, str) or not value:
        raise ToolConfigError("mcp server '%s' cwd must be an absolute existing directory" % name)
    candidate = Path(value)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ToolConfigError("mcp server '%s' cwd must be an absolute existing directory" % name)
    return str(candidate)


def _url(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ToolConfigError("mcp server '%s' url must be a non-empty https or loopback http URL" % name)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ToolConfigError("mcp server '%s' url has an invalid host or port" % name) from None
    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise ToolConfigError("mcp server '%s' url must use https, or http only for a loopback host" % name)
    if parts.username or parts.password:
        raise ToolConfigError("mcp server '%s' url must not embed credentials; name a token variable instead" % name)
    if parts.query or parts.fragment:
        raise ToolConfigError("mcp server '%s' url must not carry a query or fragment" % name)
    host = (parts.hostname or '').lower().strip('.')
    if not host:
        raise ToolConfigError("mcp server '%s' url must name a host" % name)
    if port is not None and not 0 < port < 65536:
        raise ToolConfigError("mcp server '%s' url has an invalid port" % name)
    if scheme == 'http' and not _is_loopback(host):
        raise ToolConfigError("mcp server '%s' plain http is only allowed for a loopback host; use https" % name)
    return scheme, host


def _is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _required_commands(value, caller_env, root):
    if not isinstance(value, list):
        raise ToolConfigError('required_commands must be a list of command names or absolute executable paths')
    resolved = {}
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolConfigError('required_commands must be non-empty strings')
        if item in resolved:
            raise ToolConfigError('required_commands must not repeat an entry')
        resolved[item] = _resolve(item, caller_env, 'required command', root)
    return resolved


def _resolve(value, caller_env, label, root):
    if not isinstance(value, str) or not value.strip():
        raise ToolConfigError('%s must be a command name or an absolute executable path' % label)
    if os.sep in value or value.startswith('.'):
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ToolConfigError('%s %r must be a bare command name or an absolute path' % (label, value))
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ToolConfigError('%s %s is not an executable file; install it or fix the manifest path'
                                  % (label, value))
        return str(candidate)
    search = _absolute_search_path(caller_env.get('PATH'), root)
    if not search:
        raise ToolConfigError('PATH must be a non-empty value in the caller environment to resolve %s %s'
                              % (label, value))
    found = shutil.which(value, path=search)
    if not found:
        raise ToolConfigError('%s %s was not found on the supplied PATH; install it or name an absolute path'
                              % (label, value))
    if not os.path.isabs(found):
        found = os.path.normpath(os.path.join(str(root), found))
    return found


def _absolute_search_path(search, root):
    """Return *search* with every entry absolute so resolution never depends on the process cwd."""
    if not isinstance(search, str) or not search.strip():
        return ''
    entries = []
    for entry in search.split(os.pathsep):
        if not os.path.isabs(entry):
            entry = os.path.normpath(os.path.join(str(root), entry or '.'))
        entries.append(entry)
    return os.pathsep.join(entries)


def _check_override_key(key):
    if not key.startswith('mcp_servers.'):
        raise ToolConfigError('tool overrides may only set mcp_servers entries')
    for segment in key.split('.'):
        if segment in ('model', 'model_provider', 'model_providers', 'provider', 'sandbox', 'sandbox_mode'):
            raise ToolConfigError('tool overrides may not change routing or sandbox settings')


def _prompt(servers, required):
    lines = ['Configured MCP servers:']
    if not servers:
        lines.append('- none')
    for name in sorted(servers):
        meta = servers[name]
        lines.append('- %s: %s (%s)' % (name, ', '.join(meta['enabled_tools']), meta['transport']))
    if required:
        lines.append('Required commands: ' + ', '.join(sorted(required)))
    return '\n'.join(lines)


def _env_mapping(env):
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ToolConfigError('env must be a mapping of environment variable names to values')
    return dict(env)


def _worktree(worktree):
    root = Path(worktree)
    if not root.is_dir():
        raise ToolConfigError('the task worktree must be an existing directory')
    return root


def _read(path):
    target = Path(path)
    try:
        return target.read_bytes()
    except OSError as exc:
        raise ToolConfigError('tools manifest %s could not be read (%s); provide a readable JSON file'
                              % (target, exc.strerror or 'unreadable')) from exc


def _parse(raw):
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ToolConfigError('tools manifest must be UTF-8 JSON') from exc
    try:
        manifest = json.loads(text, object_pairs_hook=_unique_keys)
    except ToolConfigError:
        raise
    except ValueError as exc:
        raise ToolConfigError('tools manifest must be valid JSON: %s' % (exc.msg,)) from exc
    if not isinstance(manifest, dict):
        raise ToolConfigError('tools manifest must be a JSON object')
    return manifest


def _unique_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ToolConfigError('tools manifest repeats the key %r' % (key,))
        seen.add(key)
    return dict(pairs)


def observed_tool_calls(text):
    """Explicit ``mcp_tool_call`` items only: never a recursive search through payloads.

    The documented Codex shape names the tool on the item itself (``item.server`` and
    ``item.tool`` of an ``item.started``/``item.completed`` event). Text that merely quotes a
    tool name inside arguments, output or a message is content, never an observation.
    """
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('type') not in TOOL_LIFECYCLE:
            continue
        item = event.get('item')
        if not isinstance(item, dict) or item.get('type') != TOOL_ITEM_TYPE:
            continue
        server = item.get('server')
        tool = item.get('tool') if isinstance(item.get('tool'), str) else item.get('name')
        if not isinstance(server, str) or not isinstance(tool, str) or not server or not tool:
            continue
        entry = {'server': server, 'tool': tool}
        if entry not in found:
            found.append(entry)
    return found


def mark_observations(evidence, text):
    """Return *evidence* with ``observed`` set only where a configured tool actually ran.

    Observations accumulate across attempts: a retry that called nothing must not erase the
    evidence that an earlier attempt really ran a tool, so ``observed_tools`` is extended and
    deduplicated instead of rebuilt from the latest text alone. Only an explicit matching
    lifecycle entry from :func:`observed_tool_calls` ever marks anything observed.
    """
    if not isinstance(evidence, dict) or not evidence:
        return evidence
    updated = json.loads(json.dumps(evidence))
    servers = updated.get('servers')
    servers = servers if isinstance(servers, dict) else {}
    previous = updated.get('observed_tools')
    matched = [entry for entry in previous if isinstance(entry, dict)] if isinstance(previous, list) else []
    for entry in observed_tool_calls(text):
        meta = servers.get(entry['server'])
        if not isinstance(meta, dict) or entry['tool'] not in (meta.get('enabled_tools') or []):
            continue
        meta['observed'] = True
        if entry not in matched:
            matched.append(entry)
    updated['observed_tools'] = matched
    return updated
