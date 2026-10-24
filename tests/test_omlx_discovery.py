"""oMLX discovery: settings, model selection, profile, recheck, load, and the HTTP helper.

Discovery tests run against ``sp.FakeOmlx`` (built from the recorded fixtures). The HTTP helper
is exercised against a real loopback stub server so redirects, proxies, headers and the secret
hygiene are checked on actual sockets.
"""
import copy
import dataclasses
import http.server
import json
import os
import socket
import sys
import threading
import traceback
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import omlx_discovery as d  # noqa: E402

QWEN = "Qwen3.5-9B-OptiQ-4bit"


class DiscoveryCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.settings = sp.write_settings(self.root / "omlx" / "settings.json")

    def refused(self, *args, **kwargs):
        """The DiscoveryError raised by ``discover``; fails the test when none is raised."""
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, *args, **kwargs)
        return ctx.exception

    def write_raw_settings(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.settings.write_text(text, encoding="utf-8")


class Discovery(DiscoveryCase):
    def test_single_loaded_model_is_selected_with_server_limits(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(profile.model.api_model, "Qwen3.5-9B-OptiQ-4bit")
        self.assertEqual(profile.model.context_window, 262144)
        self.assertEqual(profile.model.max_output_tokens, 32768)
        self.assertTrue(profile.model.accepts_images)
        self.assertNotIn("api_key", json.dumps(profile.as_report()))

    def test_profile_carries_every_server_fact(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(profile.base_url, "http://127.0.0.1:8000")
        self.assertEqual(profile.version, "0.7.3.dev1")
        self.assertEqual(profile.model.canonical_id, QWEN)
        self.assertEqual((profile.model.model_type, profile.model.engine_type), ("vlm", "vlm"))
        self.assertIs(profile.model.thinking_default, True)
        self.assertIsNone(profile.model.reasoning_efforts)
        self.assertEqual(profile.generation_tps, 42.6)
        self.assertEqual(profile.active_requests, 0)

    def test_vision_detection_uses_model_type_or_engine_type(self):
        for model_type, engine_type, expected in (
                ("vlm", "vlm", True), ("vlm", "batched", True), ("llm", "vlm", True),
                ("llm", "batched", False), ("llm", None, False)):
            with self.subTest(model_type=model_type, engine_type=engine_type):
                fake = sp.FakeOmlx("one", overrides={"model_type": model_type,
                                                     "engine_type": engine_type})
                profile = d.discover(self.settings, fetch=fake)
                self.assertEqual(profile.model.accepts_images, expected)
                self.assertEqual(profile.model.engine_type, engine_type)

    def test_alias_is_the_api_model(self):
        fake = sp.FakeOmlx("one", overrides={"model_alias": "nine-b"})
        profile = d.discover(self.settings, fetch=fake)
        self.assertEqual(profile.model.api_model, "nine-b")
        self.assertEqual(profile.model.canonical_id, QWEN)

    def test_helpers_and_embeddings_are_never_candidates(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("helper_and_embedding"))
        self.assertIn(profile.model.model_type, d.CHAT_MODEL_TYPES)
        self.assertEqual(profile.model.canonical_id, QWEN)

    def test_helper_and_embedding_cannot_be_requested(self):
        for name in ("Example-Assistant-Drafter-4bit", "Example-Embed-0.6B-4bit"):
            with self.subTest(name=name):
                error = self.refused(name, fetch=sp.FakeOmlx("helper_and_embedding"))
                self.assertEqual(error.code, "model_unknown")
                self.assertEqual(error.candidates, (QWEN,))

    def test_two_loaded_models_need_an_explicit_choice(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("two_loaded"))
        self.assertEqual(ctx.exception.code, "model_ambiguous")
        self.assertEqual(len(ctx.exception.candidates), 2)
        chosen = ctx.exception.candidates[1]
        self.assertEqual(
            d.discover(self.settings, chosen, fetch=sp.FakeOmlx("two_loaded")).model.canonical_id,
            chosen)

    def test_requested_model_matches_by_alias(self):
        fake = sp.FakeOmlx("one", overrides={"model_alias": "nine-b"})
        profile = d.discover(self.settings, "nine-b", fetch=fake)
        self.assertEqual((profile.model.canonical_id, profile.model.api_model), (QWEN, "nine-b"))

    def test_unknown_requested_model_lists_the_loaded_candidates(self):
        error = self.refused("no-such-model", fetch=sp.FakeOmlx("two_loaded"))
        self.assertEqual(error.code, "model_unknown")
        self.assertEqual(len(error.candidates), 2)
        self.assertIn("no-such-model", str(error))

    def test_requested_model_that_is_not_loaded_is_model_not_loaded(self):
        error = self.refused(QWEN, fetch=sp.FakeOmlx("none_loaded"))
        self.assertEqual(error.code, "model_not_loaded")
        self.assertEqual(error.candidates, (QWEN,))

    def test_none_loaded_lists_what_could_be_loaded(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("none_loaded"))
        self.assertEqual(ctx.exception.code, "model_not_loaded")
        self.assertTrue(ctx.exception.candidates)

    def test_loading_server_and_fallback_are_refused(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("one", overrides={"_health": "loading"}))
        self.assertEqual(ctx.exception.code, "server_loading")
        sp.write_settings(self.settings, model_fallback=True)
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(ctx.exception.code, "model_fallback_enabled")

    def test_fallback_is_refused_before_any_request(self):
        sp.write_settings(self.settings, model_fallback=True)
        fake = sp.FakeOmlx("one")
        self.assertEqual(self.refused(fetch=fake).code, "model_fallback_enabled")
        self.assertEqual(fake.calls, [])

    def test_fallback_must_be_exactly_false(self):
        for value in (None, "false", 0):
            with self.subTest(value=value):
                self.write_raw_settings({"auth": {"api_key": sp.SECRET},
                                         "server": {"host": "127.0.0.1", "port": 8000},
                                         "model": {"model_fallback": value}})
                self.assertEqual(self.refused(fetch=sp.FakeOmlx("one")).code,
                                 "model_fallback_enabled")
        self.write_raw_settings({"auth": {"api_key": sp.SECRET},
                                 "server": {"host": "127.0.0.1", "port": 8000}})
        self.assertEqual(self.refused(fetch=sp.FakeOmlx("one")).code, "model_fallback_enabled")

    def test_unexpected_health_answers_are_server_unreachable(self):
        def odd_health(settings, path, auth):
            return (500, {"status": "error"}) if path == "/health" else sp.FakeOmlx("one")(
                settings, path, auth)

        self.assertEqual(self.refused(fetch=odd_health).code, "server_unreachable")

    def test_discovery_makes_exactly_the_three_documented_requests(self):
        fake = sp.FakeOmlx("one")
        d.discover(self.settings, fetch=fake)
        self.assertCountEqual(fake.calls, ["/health", "/v1/models/status", "/api/status"])

    def test_the_key_is_requested_only_for_the_protected_endpoints(self):
        seen = {}
        fake = sp.FakeOmlx("one")

        def spy(settings, path, auth):
            seen[path] = auth
            return fake(settings, path, auth)

        d.discover(self.settings, fetch=spy)
        self.assertEqual(seen, {"/health": False, "/v1/models/status": True,
                                "/api/status": True})

    def test_discovered_at_is_an_iso_utc_timestamp(self):
        stamp = d.discover(self.settings, fetch=sp.FakeOmlx("one")).discovered_at
        self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timedelta(0))

    def test_prefill_speed_needs_a_meaningful_sample(self):
        for tokens, expected in ((9999, None), (10000, 241.5)):
            with self.subTest(tokens=tokens):
                fake = sp.FakeOmlx("one", overrides={"_api_status": {"total_prompt_tokens": tokens}})
                profile = d.discover(self.settings, fetch=fake)
                self.assertEqual(profile.prefill_tps, expected)
                self.assertEqual(profile.server_prompt_tokens, tokens)

    def test_missing_throughput_fields_are_none(self):
        fake = sp.FakeOmlx("one", overrides={"_api_status": {
            "avg_prefill_tps": None, "avg_generation_tps": "fast", "total_prompt_tokens": None,
            "active_requests": None, "version": None}})
        profile = d.discover(self.settings, fetch=fake)
        self.assertEqual((profile.prefill_tps, profile.generation_tps), (None, None))
        self.assertEqual((profile.server_prompt_tokens, profile.active_requests), (None, None))
        self.assertIsNone(profile.version)

    def test_an_unavailable_api_status_does_not_block_discovery(self):
        fake = sp.FakeOmlx("one")

        def without_status(settings, path, auth):
            return (404, {"detail": "Not Found"}) if path == "/api/status" else fake(
                settings, path, auth)

        profile = d.discover(self.settings, fetch=without_status)
        self.assertEqual(profile.model.canonical_id, QWEN)
        self.assertIsNone(profile.version)
        self.assertIsNone(profile.prefill_tps)

    def test_an_api_status_fetch_that_raises_does_not_block_discovery(self):
        fake = sp.FakeOmlx("one")
        seen = []
        for code in ("server_unreachable", "auth_failed"):
            with self.subTest(code=code):
                seen.clear()

                def failing_status(settings, path, auth):
                    seen.append(path)
                    if path == "/api/status":
                        raise d.DiscoveryError(code, "cannot reach oMLX for /api/status")
                    return fake(settings, path, auth)

                profile = d.discover(self.settings, fetch=failing_status)
                self.assertEqual(profile.model.canonical_id, QWEN)
                self.assertEqual(profile.model.context_window, 262144)
                self.assertEqual((profile.version, profile.prefill_tps, profile.generation_tps),
                                 (None, None, None))
                self.assertEqual((profile.server_prompt_tokens, profile.active_requests),
                                 (None, None))
                self.assertEqual(seen, ["/health", "/v1/models/status", "/api/status"])

    def test_a_failing_health_or_models_fetch_still_aborts_discovery(self):
        fake = sp.FakeOmlx("one")
        for broken in ("/health", "/v1/models/status"):
            with self.subTest(path=broken):
                def failing(settings, path, auth):
                    if path == broken:
                        raise d.DiscoveryError("server_unreachable", "cannot reach oMLX")
                    return fake(settings, path, auth)

                self.assertEqual(self.refused(fetch=failing).code, "server_unreachable")

    def test_a_model_without_a_reported_context_window_is_refused(self):
        error = self.refused(fetch=sp.FakeOmlx("one", overrides={"max_context_window": None}))
        self.assertEqual(error.code, "model_unknown")
        self.assertIn("max_context_window", str(error))

    def test_unsafe_model_names_are_refused(self):
        unsafe = ({"model_alias": 'nine"b'}, {"model_alias": "nine\nb"},
                  {"model_alias": "nine-b\n"}, {"model_alias": "nine b"},
                  {"model_alias": "-nine"}, {"model_alias": "a" * 129},
                  {"model_alias": "nine\\b"}, {"id": "bad id"}, {"id": "./escape"})
        for override in unsafe:
            with self.subTest(override=override):
                error = self.refused(fetch=sp.FakeOmlx("one", overrides=override))
                self.assertEqual(error.code, "model_unknown")
                self.assertIn("not a safe model name", str(error))

    def test_safe_model_names_are_accepted(self):
        for alias in ("nine-b", "org/model:4bit@v1+x", "a", "a" * 128, "Qwen3.5_9B"):
            with self.subTest(alias=alias):
                fake = sp.FakeOmlx("one", overrides={"model_alias": alias})
                self.assertEqual(d.discover(self.settings, fetch=fake).model.api_model, alias)

    def test_key_never_appears_in_any_discovery_output(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        settings = d.read_server_settings(self.settings)
        blobs = [repr(profile), json.dumps(profile.as_report()), repr(settings), str(settings)]
        for error in (self.refused(fetch=sp.FakeOmlx("two_loaded")),
                      self.refused(fetch=sp.FakeOmlx("none_loaded"))):
            blobs += [str(error), repr(error), "".join(traceback.format_exception(
                type(error), error, error.__traceback__))]
        for blob in blobs:
            self.assertNotIn(sp.SECRET, blob)


class Settings(DiscoveryCase):
    def test_port_comes_from_settings_or_documented_default(self):
        sp.write_settings(self.settings, port=8123)
        self.assertEqual(d.read_server_settings(self.settings).base_url, "http://127.0.0.1:8123")
        sp.write_settings(self.settings, port=None)
        settings = d.read_server_settings(self.settings)
        self.assertEqual((settings.port, settings.port_source), (8000, "default"))

    def test_explicit_port_is_recorded_as_a_setting(self):
        sp.write_settings(self.settings, port=8123)
        settings = d.read_server_settings(self.settings)
        self.assertEqual((settings.port, settings.port_source), (8123, "settings"))
        self.assertEqual(d.DEFAULT_PORT, 8000)

    def test_null_port_is_treated_like_an_omitted_port(self):
        self.write_raw_settings({"auth": {"api_key": sp.SECRET},
                                 "server": {"host": "127.0.0.1", "port": None},
                                 "model": {"model_fallback": False}})
        settings = d.read_server_settings(self.settings)
        self.assertEqual((settings.port, settings.port_source), (8000, "default"))

    def test_non_loopback_host_is_refused(self):
        sp.write_settings(self.settings, host="0.0.0.0")
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.read_server_settings(self.settings)
        self.assertEqual(ctx.exception.code, "not_loopback")

    def test_every_non_loopback_spelling_is_refused(self):
        for host in ("192.168.1.20", "example.com", "::", "127.0.0.2", "localhost.example.com"):
            with self.subTest(host=host):
                sp.write_settings(self.settings, host=host)
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.read_server_settings(self.settings)
                self.assertEqual(ctx.exception.code, "not_loopback")

    def test_loopback_hosts_build_the_expected_base_url(self):
        for host, url in (("127.0.0.1", "http://127.0.0.1:8000"),
                          ("localhost", "http://localhost:8000"),
                          ("::1", "http://[::1]:8000")):
            with self.subTest(host=host):
                sp.write_settings(self.settings, host=host)
                self.assertEqual(d.read_server_settings(self.settings).base_url, url)

    def test_invalid_settings_files_are_settings_invalid(self):
        good_auth = {"api_key": sp.SECRET}
        cases = {
            "missing file": None,
            "not json": "{nope",
            "not an object": [],
            "no auth": {"server": {"host": "127.0.0.1"}},
            "no key": {"auth": {}, "server": {"host": "127.0.0.1"}},
            "blank key": {"auth": {"api_key": "   "}, "server": {"host": "127.0.0.1"}},
            "non-string key": {"auth": {"api_key": 12345}, "server": {"host": "127.0.0.1"}},
            "no server": {"auth": good_auth},
            "no host": {"auth": good_auth, "server": {"port": 8000}},
            "blank host": {"auth": good_auth, "server": {"host": "", "port": 8000}},
            "string port": {"auth": good_auth, "server": {"host": "127.0.0.1", "port": "8000"}},
            "boolean port": {"auth": good_auth, "server": {"host": "127.0.0.1", "port": True}},
            "port zero": {"auth": good_auth, "server": {"host": "127.0.0.1", "port": 0}},
            "port too high": {"auth": good_auth, "server": {"host": "127.0.0.1", "port": 65536}},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                if payload is None:
                    self.settings.unlink()
                else:
                    self.write_raw_settings(payload)
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.read_server_settings(self.settings)
                self.assertEqual(ctx.exception.code, "settings_invalid")

    def test_a_key_that_is_not_a_valid_header_value_is_refused_without_echoing_it(self):
        for key in ("k9x\nInjected: 1", "k9x value", "k9x\u00e9"):
            with self.subTest(key=key):
                self.write_raw_settings({"auth": {"api_key": key},
                                         "server": {"host": "127.0.0.1", "port": 8000}})
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.read_server_settings(self.settings)
                self.assertEqual(ctx.exception.code, "settings_invalid")
                for fragment in ("k9x", "Injected"):
                    self.assertNotIn(fragment, str(ctx.exception))

    def test_the_key_is_stripped_and_kept_out_of_the_repr(self):
        self.write_raw_settings({"auth": {"api_key": " " + sp.SECRET + "\n"},
                                 "server": {"host": "127.0.0.1", "port": 8000}})
        settings = d.read_server_settings(self.settings)
        self.assertEqual(settings.api_key, sp.SECRET)
        self.assertNotIn(sp.SECRET, repr(settings))

    def test_settings_are_immutable(self):
        settings = d.read_server_settings(self.settings)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.port = 1


class SelectModel(unittest.TestCase):
    def setUp(self):
        self.two = sp.load_fixture("omlx/models_status_two_loaded.json")
        self.none = sp.load_fixture("omlx/models_status_none_loaded.json")
        self.mixed = sp.load_fixture("omlx/models_status_helper_and_embedding.json")

    def test_single_candidate_wins_without_a_request(self):
        one = sp.load_fixture("omlx/models_status_one.json")
        self.assertEqual(d.select_model(one, {}, None)["id"], QWEN)

    def test_the_servers_default_model_breaks_a_tie(self):
        chosen = d.select_model(self.two, {"default_model": "Example-Text-7B-4bit"}, None)
        self.assertEqual(chosen["id"], "Example-Text-7B-4bit")

    def test_a_default_model_that_is_not_a_candidate_does_not_break_a_tie(self):
        for default in ("Example-Missing", None, "", 12):
            with self.subTest(default=default):
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.select_model(self.two, {"default_model": default}, None)
                self.assertEqual(ctx.exception.code, "model_ambiguous")

    def test_default_model_may_be_given_as_an_alias(self):
        self.two["models"][0]["model_alias"] = "text7b"
        chosen = d.select_model(self.two, {"default_model": "text7b"}, None)
        self.assertEqual(chosen["id"], "Example-Text-7B-4bit")

    def test_an_explicit_request_beats_the_default_model(self):
        chosen = d.select_model(self.two, {"default_model": "Example-Text-7B-4bit"}, QWEN)
        self.assertEqual(chosen["id"], QWEN)

    def test_a_request_matches_an_alias(self):
        self.two["models"][0]["model_alias"] = "text7b"
        self.assertEqual(d.select_model(self.two, {}, "text7b")["id"], "Example-Text-7B-4bit")

    def test_an_id_match_beats_another_models_alias(self):
        self.two["models"][0]["model_alias"] = QWEN
        self.assertEqual(d.select_model(self.two, {}, QWEN)["id"], QWEN)

    def test_ambiguity_lists_canonical_ids_in_served_order(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.select_model(self.two, {}, None)
        self.assertEqual(ctx.exception.candidates, ("Example-Text-7B-4bit", QWEN))

    def test_unloaded_models_are_not_candidates_but_are_listed_as_loadable(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.select_model(self.none, {}, None)
        self.assertEqual((ctx.exception.code, ctx.exception.candidates), ("model_not_loaded", (QWEN,)))

    def test_only_loadable_chat_models_are_offered_for_loading(self):
        mixed = copy.deepcopy(self.mixed)
        for model in mixed["models"]:
            model["loaded"] = False
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.select_model(mixed, {}, None)
        self.assertEqual(ctx.exception.candidates, (QWEN,))

    def test_a_loading_model_is_mentioned(self):
        self.none["models"][0]["is_loading"] = True
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.select_model(self.none, {}, None)
        self.assertIn("loading", str(ctx.exception))

    def test_a_helper_of_chat_type_is_excluded_by_the_helper_flag_alone(self):
        helper = self.mixed["models"][0]
        self.assertEqual((helper["model_type"], helper["engine_type"], helper["is_helper"]),
                         ("llm", "batched", True))
        self.assertEqual(d.select_model(self.mixed, {}, None)["id"], QWEN)

    def test_non_chat_types_are_excluded(self):
        for kind in ("embedding", "reranker", "audio_stt", "audio_tts", "markitdown", None):
            with self.subTest(kind=kind):
                models = copy.deepcopy(self.mixed)
                for model in models["models"]:
                    if model["id"] == QWEN:
                        model["model_type"] = kind
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.select_model(models, {}, None)
                self.assertEqual(ctx.exception.code, "model_not_loaded")


class ServerProfileTests(DiscoveryCase):
    def profile(self):
        return d.discover(self.settings, fetch=sp.FakeOmlx("one"))

    def test_effective_context_is_the_server_value_or_a_lower_cap(self):
        profile = self.profile()
        self.assertEqual(profile.effective_context(None), 262144)
        self.assertEqual(profile.effective_context(32768), 32768)
        self.assertEqual(profile.effective_context(262144), 262144)
        self.assertEqual(profile.effective_context(10 ** 6), 262144)

    def test_effective_context_rejects_nonsense_caps(self):
        profile = self.profile()
        for cap in (0, -1, True, 1.5, "4096"):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    profile.effective_context(cap)

    def test_report_is_plain_json_with_the_model_facts(self):
        report = json.loads(json.dumps(self.profile().as_report()))
        self.assertEqual(report["base_url"], "http://127.0.0.1:8000")
        self.assertEqual(report["model"]["api_model"], QWEN)
        self.assertEqual(report["model"]["context_window"], 262144)
        self.assertEqual(report["prefill_tps"], 241.5)
        self.assertEqual(sorted(report), sorted([
            "base_url", "version", "model", "prefill_tps", "generation_tps",
            "server_prompt_tokens", "active_requests", "discovered_at"]))

    def test_profiles_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.profile().version = "x"


class Recheck(DiscoveryCase):
    def setUp(self):
        super().setUp()
        self.profile = d.discover(self.settings, fetch=sp.FakeOmlx("one"))

    def recheck(self, fetch):
        return d.recheck(self.profile, self.settings, fetch=fetch)

    def test_unchanged_server_passes_after_one_models_status_request(self):
        fake = sp.FakeOmlx("one")
        self.assertIsNone(self.recheck(fake))
        self.assertEqual(fake.calls, ["/v1/models/status"])

    def test_unloaded_model_is_model_changed(self):
        self.assertEqual(self.recheck(sp.FakeOmlx("none_loaded")), "model_changed")

    def test_shrunk_context_is_model_changed_and_a_larger_one_is_fine(self):
        shrunk = sp.FakeOmlx("one", overrides={"max_context_window": 8192})
        self.assertEqual(self.recheck(shrunk), "model_changed")
        grown = sp.FakeOmlx("one", overrides={"max_context_window": 524288})
        self.assertIsNone(self.recheck(grown))

    def test_missing_context_is_model_changed(self):
        self.assertEqual(self.recheck(sp.FakeOmlx("one", overrides={"max_context_window": None})),
                         "model_changed")

    def test_changed_alias_is_model_changed(self):
        self.assertEqual(self.recheck(sp.FakeOmlx("one", overrides={"model_alias": "renamed"})),
                         "model_changed")

    def test_pinned_model_gone_is_model_changed(self):
        self.profile = d.discover(self.settings, fetch=sp.FakeOmlx("two_loaded"),
                                  requested_model="Example-Text-7B-4bit")
        self.assertEqual(self.recheck(sp.FakeOmlx("one")), "model_changed")

    def test_other_models_loading_or_unloading_does_not_matter(self):
        self.assertIsNone(self.recheck(sp.FakeOmlx("two_loaded")))
        self.assertIsNone(self.recheck(sp.FakeOmlx("helper_and_embedding")))

    def test_fallback_switched_on_is_model_changed(self):
        sp.write_settings(self.settings, model_fallback=True)
        self.assertEqual(self.recheck(sp.FakeOmlx("one")), "model_changed")

    def test_unreachable_server_is_server_unreachable(self):
        def down(settings, path, auth):
            raise d.DiscoveryError("server_unreachable", "connection refused")

        self.assertEqual(self.recheck(down), "server_unreachable")

    def test_a_server_that_answers_with_an_error_is_server_unreachable(self):
        self.assertEqual(self.recheck(lambda s, p, a: (500, {"detail": "boom"})),
                         "server_unreachable")
        self.assertEqual(self.recheck(lambda s, p, a: (200, {"models": "nope"})),
                         "server_unreachable")

    def test_a_moved_or_unreadable_settings_file_is_server_unreachable(self):
        sp.write_settings(self.settings, port=8999)
        self.assertEqual(self.recheck(sp.FakeOmlx("one")), "server_unreachable")
        sp.write_settings(self.settings, host="0.0.0.0")
        self.assertEqual(self.recheck(sp.FakeOmlx("one")), "server_unreachable")
        self.settings.unlink()
        self.assertEqual(self.recheck(sp.FakeOmlx("one")), "server_unreachable")


class LoadModel(DiscoveryCase):
    def test_loading_makes_the_model_discoverable(self):
        fake = sp.FakeOmlx("none_loaded")
        self.assertEqual(self.refused(fetch=fake).code, "model_not_loaded")
        self.assertIsNone(d.load_model(self.settings, QWEN, fetch=fake))
        self.assertEqual(d.discover(self.settings, fetch=fake).model.canonical_id, QWEN)
        self.assertIn("/v1/models/%s/load" % QWEN, fake.calls)

    def test_unknown_model_is_model_unknown(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.load_model(self.settings, "No-Such-Model", fetch=sp.FakeOmlx("none_loaded"))
        self.assertEqual(ctx.exception.code, "model_unknown")

    def test_server_refusals_are_model_not_loaded_with_the_servers_reason(self):
        def too_big(settings, path, auth):
            return 507, {"detail": "Insufficient memory to load model"}

        with self.assertRaises(d.DiscoveryError) as ctx:
            d.load_model(self.settings, QWEN, fetch=too_big)
        self.assertEqual(ctx.exception.code, "model_not_loaded")
        self.assertIn("Insufficient memory", str(ctx.exception))
        self.assertNotIn(sp.SECRET, str(ctx.exception))

    def test_unsafe_ids_are_refused_before_any_request(self):
        fake = sp.FakeOmlx("none_loaded")
        for model_id in ("bad id", "x\ny", "", "./x"):
            with self.subTest(model_id=model_id):
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.load_model(self.settings, model_id, fetch=fake)
                self.assertEqual(ctx.exception.code, "model_unknown")
        self.assertEqual(fake.calls, [])


class FakeOmlxBehaviour(unittest.TestCase):
    """The fake has to behave like the server for the tests built on it to mean anything."""

    def setUp(self):
        self.settings = d.ServerSettings("127.0.0.1", 8000, sp.SECRET, False, "settings")

    def test_serves_the_recorded_endpoints_and_records_paths(self):
        fake = sp.FakeOmlx("one")
        status, health = fake(self.settings, "/health", False)
        self.assertEqual((status, health["status"]), (200, "healthy"))
        status, models = fake(self.settings, "/v1/models/status", True)
        self.assertEqual((status, models["models"][0]["id"]), (200, QWEN))
        self.assertEqual(fake(self.settings, "/api/status", True)[1]["version"], "0.7.3.dev1")
        self.assertEqual(fake.calls, ["/health", "/v1/models/status", "/api/status"])

    def test_loading_health_is_a_503(self):
        status, body = sp.FakeOmlx("one", overrides={"_health": "loading"})(
            self.settings, "/health", False)
        self.assertEqual((status, body["status"]), (503, "loading"))

    def test_protected_endpoints_need_the_right_key(self):
        fake = sp.FakeOmlx("one")
        for path in ("/v1/models/status", "/api/status"):
            with self.assertRaises(d.DiscoveryError) as ctx:
                fake(self.settings, path, False)
            self.assertEqual(ctx.exception.code, "auth_failed")
        wrong = d.ServerSettings("127.0.0.1", 8000, "another-key", False, "settings")
        with self.assertRaises(d.DiscoveryError) as ctx:
            fake(wrong, "/api/status", True)
        self.assertEqual(ctx.exception.code, "auth_failed")

    def test_unknown_paths_are_404(self):
        self.assertEqual(sp.FakeOmlx("one")(self.settings, "/nope", True)[0], 404)

    def test_api_status_counts_follow_the_variant(self):
        for variant, loaded in (("one", 1), ("none_loaded", 0), ("two_loaded", 2),
                                ("helper_and_embedding", 3)):
            with self.subTest(variant=variant):
                body = sp.FakeOmlx(variant)(self.settings, "/api/status", True)[1]
                self.assertEqual((body["models_loaded"], len(body["loaded_models"])),
                                 (loaded, loaded))

    def test_overrides_touch_only_the_loaded_chat_models(self):
        fake = sp.FakeOmlx("helper_and_embedding", overrides={"max_context_window": 1234,
                                                              "model_alias": "q"})
        models = {m["id"]: m for m in fake(self.settings, "/v1/models/status", True)[1]["models"]}
        self.assertEqual(models[QWEN]["max_context_window"], 1234)
        self.assertEqual(models[QWEN]["model_alias"], "q")
        for other in ("Example-Assistant-Drafter-4bit", "Example-Embed-0.6B-4bit"):
            self.assertEqual(models[other]["max_context_window"], 8192)
            self.assertNotIn("model_alias", models[other])

    def test_instances_do_not_share_state(self):
        first, second = sp.FakeOmlx("none_loaded"), sp.FakeOmlx("none_loaded")
        first(self.settings, "/v1/models/%s/load" % QWEN, True)

        def loaded(fake):
            return fake(self.settings, "/v1/models/status", True)[1]["models"][0]["loaded"]

        self.assertEqual((loaded(first), loaded(second)), (True, False))


class StubOmlx(http.server.ThreadingHTTPServer):
    """A real loopback HTTP server that serves canned answers and records every request."""

    daemon_threads = True

    def __init__(self, routes):
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.routes, self.requests = routes, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()


class StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def serve(self):
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization")))
        status, body, headers = self.server.routes.get(
            (self.command, self.path), (404, {"detail": "Not Found"}, {}))
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = serve


class HttpFetchBehaviour(DiscoveryCase):
    def serve(self, routes):
        stub = StubOmlx(routes)
        self.addCleanup(stub.stop)
        sp.write_settings(self.settings, port=stub.server_port)
        return stub, d.read_server_settings(self.settings)

    def test_the_key_is_sent_only_when_asked(self):
        stub, settings = self.serve({("GET", "/health"): (200, {"status": "healthy"}, {}),
                                     ("GET", "/api/status"): (200, {"version": "x"}, {})})
        self.assertEqual(d.http_fetch(settings, "/health", False), (200, {"status": "healthy"}))
        self.assertEqual(d.http_fetch(settings, "/api/status", True), (200, {"version": "x"}))
        self.assertEqual(stub.requests, [("GET", "/health", None),
                                         ("GET", "/api/status", "Bearer " + sp.SECRET)])

    def test_other_error_statuses_come_back_with_their_body(self):
        _, settings = self.serve({("GET", "/health"): (503, {"status": "loading"}, {}),
                                  ("GET", "/gone"): (404, b"not json at all", {})})
        self.assertEqual(d.http_fetch(settings, "/health", False), (503, {"status": "loading"}))
        self.assertEqual(d.http_fetch(settings, "/gone", False), (404, None))

    def test_401_and_403_are_auth_failed(self):
        _, settings = self.serve({("GET", "/api/status"): (401, {"detail": "no"}, {}),
                                  ("GET", "/v1/models/status"): (403, {"detail": "no"}, {})})
        for path in ("/api/status", "/v1/models/status"):
            with self.subTest(path=path):
                with self.assertRaises(d.DiscoveryError) as ctx:
                    d.http_fetch(settings, path, True)
                self.assertEqual(ctx.exception.code, "auth_failed")

    def test_redirects_are_refused_not_followed(self):
        stub, settings = self.serve({
            ("GET", "/health"): (302, b"", {"Location": "/elsewhere"}),
            ("GET", "/elsewhere"): (200, {"status": "healthy"}, {})})
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.http_fetch(settings, "/health", True)
        self.assertEqual(ctx.exception.code, "server_unreachable")
        self.assertIn("redirect", str(ctx.exception))
        self.assertEqual([path for _, path, _ in stub.requests], ["/health"])

    def test_the_opener_disables_proxies_and_refuses_redirects(self):
        opener = d.build_opener()
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
                             for h in opener.handlers))
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://corp:3128"}):
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
                                 for h in d.build_opener().handlers))
        self.assertIsNone(d._RefuseRedirects().redirect_request(
            None, None, 302, "", None, "http://evil.example"))

    def test_environment_proxies_are_ignored(self):
        stub, settings = self.serve({("GET", "/health"): (200, {"status": "healthy"}, {})})
        proxy = "http://127.0.0.1:%d" % self.closed_port()
        env = {name: proxy for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(d.http_fetch(settings, "/health", False)[0], 200)
        self.assertEqual(len(stub.requests), 1)

    def test_connection_failure_is_server_unreachable(self):
        sp.write_settings(self.settings, port=self.closed_port())
        settings = d.read_server_settings(self.settings)
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.http_fetch(settings, "/health", False)
        self.assertEqual(ctx.exception.code, "server_unreachable")
        self.assertIn(settings.base_url, str(ctx.exception))

    def test_a_successful_answer_that_is_not_json_is_server_unreachable(self):
        _, settings = self.serve({("GET", "/health"): (200, b"<html>hi</html>", {})})
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.http_fetch(settings, "/health", False)
        self.assertEqual(ctx.exception.code, "server_unreachable")

    def test_requests_time_out_after_fifteen_seconds(self):
        seen = {}

        class Opener:
            def open(self, request, timeout=None):
                seen["timeout"], seen["url"] = timeout, request.full_url
                raise socket.timeout("timed out")

        settings = d.read_server_settings(self.settings)
        with mock.patch.object(d, "build_opener", return_value=Opener()):
            with self.assertRaises(d.DiscoveryError) as ctx:
                d.http_fetch(settings, "/health", False)
        self.assertEqual(ctx.exception.code, "server_unreachable")
        self.assertEqual((d.HTTP_TIMEOUT_SECONDS, seen["timeout"]), (15, 15))
        self.assertEqual(seen["url"], "http://127.0.0.1:8000/health")

    def test_loading_a_model_posts_to_the_quoted_load_path(self):
        stub, settings = self.serve({
            ("POST", "/v1/models/Example-Model%3A4bit%40x/load"): (200, {"status": "ok"}, {}),
            ("POST", "/v1/models/org%2Fmodel/load"): (200, {"status": "ok"}, {})})
        self.assertIsNone(d.load_model(self.settings, "Example-Model:4bit@x"))
        self.assertIsNone(d.load_model(self.settings, "org/model"))
        self.assertEqual(stub.requests, [
            ("POST", "/v1/models/Example-Model%3A4bit%40x/load", "Bearer " + sp.SECRET),
            ("POST", "/v1/models/org%2Fmodel/load", "Bearer " + sp.SECRET)])

    def test_discovery_over_real_http_reads_the_three_endpoints(self):
        stub, _ = self.serve({
            ("GET", "/health"): (200, sp.load_fixture("omlx/health_ok.json"), {}),
            ("GET", "/v1/models/status"): (200, sp.load_fixture("omlx/models_status_one.json"), {}),
            ("GET", "/api/status"): (200, sp.load_fixture("omlx/api_status.json"), {})})
        profile = d.discover(self.settings)
        self.assertEqual(profile.model.api_model, QWEN)
        self.assertEqual(profile.base_url, "http://127.0.0.1:%d" % stub.server_port)
        self.assertEqual(stub.requests, [
            ("GET", "/health", None),
            ("GET", "/v1/models/status", "Bearer " + sp.SECRET),
            ("GET", "/api/status", "Bearer " + sp.SECRET)])

    def test_discovery_survives_an_api_status_answer_that_is_not_json(self):
        stub, _ = self.serve({
            ("GET", "/health"): (200, sp.load_fixture("omlx/health_ok.json"), {}),
            ("GET", "/v1/models/status"): (200, sp.load_fixture("omlx/models_status_one.json"), {}),
            ("GET", "/api/status"): (200, b"<html>maintenance</html>", {})})
        profile = d.discover(self.settings)
        self.assertEqual(profile.model.api_model, QWEN)
        self.assertEqual((profile.version, profile.prefill_tps, profile.generation_tps),
                         (None, None, None))
        self.assertEqual(stub.requests, [
            ("GET", "/health", None),
            ("GET", "/v1/models/status", "Bearer " + sp.SECRET),
            ("GET", "/api/status", "Bearer " + sp.SECRET)])

    def test_discovery_survives_an_api_status_connection_that_dies(self):
        # The stub closes the socket without an answer for /api/status only.
        routes = {
            ("GET", "/health"): (200, sp.load_fixture("omlx/health_ok.json"), {}),
            ("GET", "/v1/models/status"): (200, sp.load_fixture("omlx/models_status_one.json"), {})}
        stub, _ = self.serve(routes)
        original = StubHandler.serve

        def serve(handler):
            if handler.path == "/api/status":
                handler.server.requests.append((handler.command, handler.path,
                                                handler.headers.get("Authorization")))
                handler.connection.shutdown(socket.SHUT_RDWR)
                return
            original(handler)

        with mock.patch.object(StubHandler, "do_GET", serve):
            profile = d.discover(self.settings)
        self.assertEqual(profile.model.api_model, QWEN)
        self.assertIsNone(profile.version)
        self.assertEqual([path for _, path, _ in stub.requests],
                         ["/health", "/v1/models/status", "/api/status"])

    def test_only_the_blocking_model_load_gets_the_long_timeout(self):
        seen = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        class Opener:
            def open(self, request, timeout=None):
                seen.append((request.get_method(), request.selector, timeout))
                return Answer()

        settings = d.read_server_settings(self.settings)
        paths = ("/health", "/api/status", "/v1/models/status",
                 "/v1/models/%s/load" % QWEN, "/v1/models/%s/unload" % QWEN)
        with mock.patch.object(d, "build_opener", return_value=Opener()):
            for path in paths:
                d.http_fetch(settings, path, True)
        self.assertEqual(seen, [
            ("GET", "/health", 15), ("GET", "/api/status", 15), ("GET", "/v1/models/status", 15),
            ("POST", "/v1/models/%s/load" % QWEN, 900),
            ("GET", "/v1/models/%s/unload" % QWEN, 15)])
        self.assertEqual((d.HTTP_TIMEOUT_SECONDS, d.LOAD_TIMEOUT_SECONDS), (15, 900))

    def test_load_model_waits_with_the_load_timeout(self):
        seen = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"status": "ok"}'

        class Opener:
            def open(self, request, timeout=None):
                seen.append((request.get_method(), request.selector, timeout))
                return Answer()

        with mock.patch.object(d, "build_opener", return_value=Opener()):
            self.assertIsNone(d.load_model(self.settings, QWEN))
        self.assertEqual(seen, [("POST", "/v1/models/%s/load" % QWEN, 900)])

    def test_the_key_never_appears_in_any_error(self):
        stub, settings = self.serve({
            ("GET", "/unauthorised"): (401, {"detail": "bad key"}, {}),
            ("GET", "/redirect"): (302, b"", {"Location": "/x"}),
            ("GET", "/html"): (200, b"<html>", {})})
        down = d.ServerSettings("127.0.0.1", self.closed_port(), sp.SECRET, False, "settings")
        failures = []
        for target, path in ((settings, "/unauthorised"), (settings, "/redirect"),
                             (settings, "/html"), (down, "/health")):
            with self.assertRaises(d.DiscoveryError) as ctx:
                d.http_fetch(target, path, True)
            failures.append(ctx.exception)
        for error in failures:
            for blob in (str(error), repr(error), "".join(traceback.format_exception(
                    type(error), error, error.__traceback__))):
                self.assertNotIn(sp.SECRET, blob)

    @staticmethod
    def closed_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


class DiscoveryErrorShape(unittest.TestCase):
    def test_error_carries_code_message_and_candidates(self):
        error = d.DiscoveryError("model_ambiguous", "pick one", ("a", "b"))
        self.assertEqual((error.code, str(error), error.candidates), ("model_ambiguous", "pick one", ("a", "b")))
        self.assertEqual(d.DiscoveryError("server_loading", "wait").candidates, ())

    def test_candidates_are_stored_as_a_tuple_of_strings(self):
        self.assertEqual(d.DiscoveryError("x", "y", ["a", "b"]).candidates, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
