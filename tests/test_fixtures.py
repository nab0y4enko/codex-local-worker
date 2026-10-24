"""Recorded oMLX and Codex fixtures: hygiene, shape, and the script that captures them.

No test here talks to a server or a model; the capture script is exercised with an injected
fetch function.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import support as sp  # noqa: E402
import capture_fixtures as cap  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
OMLX_FILES = ("health_ok", "health_loading", "api_status", "models_status_one",
              "models_status_none_loaded", "models_status_two_loaded",
              "models_status_helper_and_embedding")
CODEX_FILES = ("exec_pilot.jsonl", "debug_models.json", "rollout_planner.jsonl")
CHAT_TYPES = ("llm", "vlm")
# The repository is public: no fixture may name a person, a home directory or a scratch path.
PERSONAL_MARKERS = ("oleksii", "/users/", "/private/tmp/claude")


def loaded(status, *, chat_only=False):
    return [m for m in status["models"] if m["loaded"]
            and (not chat_only or m["model_type"] in CHAT_TYPES)]


class FixtureHygiene(unittest.TestCase):
    def test_fixtures_exist_and_hold_no_secrets(self):
        root = Path(__file__).parent / "fixtures"
        for rel in ("omlx/models_status_one.json", "omlx/api_status.json",
                    "codex/exec_pilot.jsonl", "codex/debug_models.json"):
            text = (root / rel).read_text()
            self.assertNotRegex(text, r"(?i)bearer\s+\S{8,}|api_key\"\s*:\s*\"[^\"<]")
            self.assertNotIn(os.path.expanduser("~"), text)

    def test_no_fixture_file_carries_a_personal_or_scratch_path(self):
        files = [path for path in FIXTURES.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts]
        self.assertGreaterEqual(len(files), len(OMLX_FILES) + len(CODEX_FILES))
        for path in files:
            text = path.read_bytes().decode("utf-8", "replace").lower()
            for marker in PERSONAL_MARKERS:
                if marker in text:   # fail without echoing the file's content
                    self.fail("{0} contains {1!r}".format(path.relative_to(FIXTURES), marker))

    def test_models_status_one_has_one_loaded_chat_model(self):
        status = sp.load_fixture("omlx/models_status_one.json")
        loaded = [m for m in status["models"] if m["loaded"]]
        self.assertEqual(len(loaded), 1)
        self.assertIn(loaded[0]["model_type"], ("llm", "vlm"))
        self.assertIsInstance(loaded[0]["max_context_window"], int)

    def test_every_fixture_file_exists_and_is_clean(self):
        paths = ["omlx/{0}.json".format(name) for name in OMLX_FILES] + \
                ["codex/{0}".format(name) for name in CODEX_FILES]
        for rel in paths:
            text = sp.load_fixture_text(rel)
            self.assertTrue(text.strip(), rel)
            self.assertNotIn(os.path.expanduser("~"), text, rel)
            for key in cap.DROP_KEYS:
                self.assertNotIn('"{0}"'.format(key), text, rel)
            self.assertNotRegex(text, r"(?i)bearer\s+\S{8,}", rel)

    def test_load_fixture_decodes_json_and_lists_jsonl_lines(self):
        self.assertIsInstance(sp.load_fixture("omlx/models_status_one.json"), dict)
        lines = sp.load_fixture("codex/exec_pilot.jsonl")
        self.assertIsInstance(lines, list)
        self.assertEqual(lines[0]["type"], "thread.started")
        self.assertEqual(lines[-1]["type"], "turn.completed")
        raw = sp.load_fixture_text("codex/exec_pilot.jsonl")
        self.assertEqual(len(raw.splitlines()), len(lines))


class OmlxFixtureShapes(unittest.TestCase):
    def test_real_status_shape_and_the_one_loaded_model(self):
        status = sp.load_fixture("omlx/models_status_one.json")
        for key in ("final_ceiling", "current_model_memory", "model_count", "loaded_count",
                    "load_seconds_per_gb_estimate", "load_time_observations", "models"):
            self.assertIn(key, status)
        self.assertEqual(status["model_count"], len(status["models"]))
        self.assertEqual(status["loaded_count"], len(loaded(status)))
        model = loaded(status)[0]
        for key in ("id", "loaded", "is_loading", "pinned", "engine_type", "model_type",
                    "model_context_length", "max_context_window", "max_tokens", "is_helper",
                    "thinking_default", "reasoning_efforts"):
            self.assertIn(key, model)
        self.assertEqual(model["id"], "Qwen3.5-9B-OptiQ-4bit")
        self.assertFalse(model["is_helper"])

    def test_health_and_api_status_shapes(self):
        ok = sp.load_fixture("omlx/health_ok.json")
        self.assertEqual(ok["status"], "healthy")
        self.assertIn("default_model", ok)
        self.assertIn("engine_pool", ok)
        self.assertNotIn("_http_status", ok)
        status = sp.load_fixture("omlx/api_status.json")
        for key in ("version", "loaded_models", "models_loading", "active_requests",
                    "waiting_requests", "total_prompt_tokens", "avg_prefill_tps",
                    "avg_generation_tps"):
            self.assertIn(key, status)

    def test_health_loading_records_the_503(self):
        loading = sp.load_fixture("omlx/health_loading.json")
        self.assertEqual(loading["_http_status"], 503)
        self.assertEqual(loading["status"], "loading")
        ok = sp.load_fixture("omlx/health_ok.json")
        self.assertEqual(set(loading) - {"_http_status"}, set(ok))

    def test_none_loaded_keeps_the_discovered_models_unloaded(self):
        one = sp.load_fixture("omlx/models_status_one.json")
        none = sp.load_fixture("omlx/models_status_none_loaded.json")
        self.assertEqual([m["id"] for m in none["models"]], [m["id"] for m in one["models"]])
        self.assertEqual(loaded(none), [])
        self.assertEqual(none["loaded_count"], 0)
        self.assertEqual(none["model_count"], len(none["models"]))

    def test_two_loaded_adds_a_second_chat_model(self):
        two = sp.load_fixture("omlx/models_status_two_loaded.json")
        chat = loaded(two, chat_only=True)
        self.assertEqual(len(chat), 2)
        self.assertEqual(len({m["id"] for m in chat}), 2)
        second = [m for m in chat if m["id"] != "Qwen3.5-9B-OptiQ-4bit"][0]
        self.assertEqual((second["model_type"], second["engine_type"]), ("llm", "batched"))
        self.assertIsInstance(second["max_context_window"], int)
        self.assertEqual(two["loaded_count"], len(loaded(two)))
        self.assertEqual(two["model_count"], len(two["models"]))

    def test_helper_and_embedding_are_loaded_next_to_the_real_chat_model(self):
        status = sp.load_fixture("omlx/models_status_helper_and_embedding.json")
        models = loaded(status)
        self.assertEqual(len(loaded(status, chat_only=True)), 2)
        chat = [m for m in loaded(status, chat_only=True) if not m["is_helper"]]
        self.assertEqual([m["id"] for m in chat], ["Qwen3.5-9B-OptiQ-4bit"])
        self.assertEqual(len([m for m in models if m["is_helper"]]), 1)
        self.assertEqual(len([m for m in models if m["model_type"] == "embedding"]), 1)
        self.assertEqual(status["loaded_count"], len(models))
        self.assertEqual(status["model_count"], len(status["models"]))


class CodexFixtureShapes(unittest.TestCase):
    def test_catalog_is_trimmed_to_the_listed_fields(self):
        catalog = sp.load_fixture("codex/debug_models.json")
        self.assertIn("gpt-6-luna", [m["slug"] for m in catalog["models"]])
        for model in catalog["models"]:
            self.assertEqual(set(model), set(cap.CATALOG_FIELDS))
            self.assertTrue(model["description"])
            self.assertIsInstance(model["context_window"], int)
        self.assertNotIn("base_instructions", sp.load_fixture_text("codex/debug_models.json"))

    def test_pilot_stream_is_a_real_codex_exec_json_stream(self):
        events = sp.load_fixture("codex/exec_pilot.jsonl")
        kinds = {event["type"] for event in events}
        self.assertTrue({"thread.started", "turn.started", "item.completed",
                         "turn.completed"} <= kinds)
        usage = events[-1]["usage"]
        self.assertEqual(usage["input_tokens"], 47610)
        self.assertEqual(usage["cached_input_tokens"], 39169)
        self.assertEqual(usage["output_tokens"], 627)
        self.assertEqual(usage["reasoning_output_tokens"], 190)

    def test_planner_rollout_has_meta_and_growing_token_counts(self):
        lines = sp.load_fixture("codex/rollout_planner.jsonl")
        self.assertEqual(lines[0]["type"], "session_meta")
        self.assertEqual(lines[0]["payload"]["id"], "019a0000-0000-7000-8000-000000000001")
        counts = [line["payload"]["info"]["total_token_usage"] for line in lines
                  if line["type"] == "event_msg" and line["payload"]["type"] == "token_count"]
        self.assertEqual(len(counts), 3)
        totals = [usage["total_tokens"] for usage in counts]
        self.assertEqual(totals, sorted(set(totals)))
        for field in ("input_tokens", "cached_input_tokens", "output_tokens",
                      "reasoning_output_tokens"):
            values = [usage[field] for usage in counts]
            self.assertEqual(values, sorted(values), field)
            self.assertGreater(values[-1], values[0], field)


class CaptureScript(unittest.TestCase):
    HOME = "/Users/someone"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def settings(self, **server):
        path = self.root / "settings.json"
        body = {"server": dict({"host": "127.0.0.1", "port": 8123}, **server),
                "auth": {"api_key": "capture-test-key-0123456789"}}
        for key in [k for k, v in body["server"].items() if v is None]:
            del body["server"][key]
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def test_sanitize_drops_credential_keys_and_replaces_the_home_path(self):
        data = {"api_key": "k", "secret_key": "s", "sub_keys": [{"key": "x"}],
                "models": [{"path": self.HOME + "/.omlx/models/m", "api_key": "k",
                            "nested": {"sub_keys": [], "n": 3}}],
                "note": "see " + self.HOME + "/x"}
        self.assertEqual(cap.sanitize(data, self.HOME), {
            "models": [{"path": "~/.omlx/models/m", "nested": {"n": 3}}], "note": "see ~/x"})

    def test_sanitize_replaces_scratch_paths_with_a_neutral_placeholder(self):
        scratch = "/private/tmp/claude-502/-Users-someone--proj/abc/scratchpad/pilot1"
        text = ("set `x = true` in {0}/codexhome/config.toml. and also {0}/state/report.json"
                .format(scratch))
        self.assertEqual(cap.sanitize_text(text, self.HOME),
                         "set `x = true` in <scratch>/codex-home/config.toml. and also <scratch>")
        self.assertEqual(cap.sanitize({"a": [scratch + "/codexhome"]}, self.HOME),
                         {"a": ["<scratch>/codex-home"]})

    def test_trim_catalog_keeps_only_the_fields_the_plugin_reads(self):
        raw = {"models": [{"slug": "a", "display_name": "A", "description": "d",
                           "context_window": 5, "supported_reasoning_levels": [],
                           "default_reasoning_level": "low", "visibility": "list",
                           "base_instructions": "x" * 100, "priority": 1}]}
        trimmed = cap.trim_catalog(raw)
        self.assertEqual(list(trimmed), ["models"])
        self.assertEqual(set(trimmed["models"][0]), set(cap.CATALOG_FIELDS))

    def test_server_settings_are_loopback_only_and_port_falls_back_to_8000(self):
        self.assertEqual(cap.read_omlx_settings(self.settings())[0], "http://127.0.0.1:8123")
        self.assertEqual(cap.read_omlx_settings(self.settings(port=None))[0],
                         "http://127.0.0.1:8000")
        with self.assertRaises(SystemExit):
            cap.read_omlx_settings(self.settings(host="0.0.0.0"))

    def fake_fetch(self, health_status=200):
        def fetch(base_url, path, key):
            self.assertTrue(base_url.startswith("http://127.0.0.1:"))
            if path == "/health":
                self.assertIsNone(key)
                return health_status, {"status": "healthy", "default_model": "m",
                                       "engine_pool": {}, "mcp": None}
            self.assertEqual(key, "capture-test-key-0123456789")
            if path == "/api/status":
                return 200, {"version": "1.0", "loaded_models": ["m"], "api_key": "leak",
                             "model_dir": self.HOME + "/.omlx/models"}
            return 200, {"models": [{"id": "m", "loaded": True,
                                     "path": self.HOME + "/.omlx/models/m"}]}
        return fetch

    def capture(self, fetch, pilot_extra="elsewhere"):
        pilot = self.root / "attempt-1.jsonl"
        pilot.write_text('{"type":"thread.started","thread_id":"t"}\n'
                         '{"type":"item.completed","item":{"text":"' + self.HOME + '/repo"}}\n'
                         '{"type":"item.completed","item":{"text":"' + pilot_extra + '"}}\n',
                         encoding="utf-8")
        models = self.root / "models.json"
        models.write_text(json.dumps({"models": [{"slug": "a", "display_name": "A",
                          "description": "d", "context_window": 5,
                          "supported_reasoning_levels": [], "default_reasoning_level": "low",
                          "visibility": "list", "base_instructions": "drop me"}]}),
                          encoding="utf-8")
        out = self.root / "out"
        cap.capture(out, settings_path=self.settings(), pilot_attempt=pilot,
                    codex_models_json=models, fetch=fetch, home=self.HOME)
        return out

    def test_capture_writes_clean_files(self):
        out = self.capture(self.fake_fetch())
        status = json.loads((out / "omlx" / "api_status.json").read_text())
        self.assertNotIn("api_key", status)
        self.assertEqual(status["model_dir"], "~/.omlx/models")
        models = json.loads((out / "omlx" / "models_status_one.json").read_text())
        self.assertEqual(models["models"][0]["path"], "~/.omlx/models/m")
        self.assertEqual(json.loads((out / "omlx" / "health_ok.json").read_text())["status"],
                         "healthy")
        self.assertNotIn(self.HOME, (out / "codex" / "exec_pilot.jsonl").read_text())
        catalog = json.loads((out / "codex" / "debug_models.json").read_text())
        self.assertNotIn("base_instructions", catalog["models"][0])
        for path in out.rglob("*.json*"):
            self.assertNotIn("capture-test-key", path.read_text())

    def test_capture_refuses_a_non_healthy_server_and_never_writes_the_key(self):
        with self.assertRaises(SystemExit) as ctx:
            self.capture(self.fake_fetch(health_status=503))
        self.assertNotIn("capture-test-key", str(ctx.exception))

    def test_capture_scrubs_scratch_paths_from_the_pilot_stream(self):
        out = self.capture(self.fake_fetch(), pilot_extra=(
            "/private/tmp/claude-502/-Users-someone--p/x/scratchpad/pilot1/codexhome"))
        text = (out / "codex" / "exec_pilot.jsonl").read_text()
        self.assertIn("<scratch>/codex-home", text)
        self.assertNotIn("/private/tmp/claude", text)
        for line in text.splitlines():
            json.loads(line)

    def test_capture_refuses_output_that_contains_the_key(self):
        def leaking(base_url, path, key):
            status, body = self.fake_fetch()(base_url, path, key)
            if path == "/api/status":
                body["note"] = key
            return status, body
        with self.assertRaises(SystemExit) as ctx:
            self.capture(leaking)
        self.assertNotIn("capture-test-key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
