"""The generated Codex model catalog: the qualified template filled from the discovered model.

Offline: the profile comes from recorded oMLX fixtures (``sp.discovered_profile``) and the
catalog is written to a disposable directory.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import codex_catalog  # noqa: E402


class LocalEntry(unittest.TestCase):
    def setUp(self):
        self.profile = sp.discovered_profile()

    def test_entry_fills_the_template_from_the_profile(self):
        entry = codex_catalog.build_local_entry(self.profile.model, 32768)
        self.assertEqual(entry["slug"], "Qwen3.5-9B-OptiQ-4bit")
        self.assertEqual(entry["context_window"], 32768)
        self.assertIn("image", entry["input_modalities"])

    def test_every_placeholder_becomes_a_typed_value(self):
        entry = codex_catalog.build_local_entry(self.profile.model, 32768)
        self.assertEqual(entry["display_name"], self.profile.model.api_model)
        self.assertIs(type(entry["context_window"]), int)
        self.assertEqual(entry["input_modalities"], ["text", "image"])
        self.assertNotIn("{{", json.dumps(entry))

    def test_the_slug_is_the_name_the_worker_asks_for(self):
        aliased = sp.discovered_profile(overrides={"model_alias": "nine-b"})
        entry = codex_catalog.build_local_entry(aliased.model, 4096)
        self.assertEqual((entry["slug"], entry["display_name"]), ("nine-b", "nine-b"))

    def test_a_text_only_model_takes_text_only(self):
        text = sp.discovered_profile(overrides={"engine_type": "batched", "model_type": "llm"})
        self.assertFalse(text.model.accepts_images)
        entry = codex_catalog.build_local_entry(text.model, 32768)
        self.assertEqual(entry["input_modalities"], ["text"])

    def test_the_qualified_fields_are_kept_as_the_spike_left_them(self):
        entry = codex_catalog.build_local_entry(self.profile.model, 32768)
        template = json.loads(codex_catalog.TEMPLATE_PATH.read_text(encoding="utf-8"))
        for key in ("shell_type", "apply_patch_tool_type", "tool_mode", "use_responses_lite",
                    "truncation_policy", "supported_reasoning_levels", "base_instructions"):
            with self.subTest(key=key):
                self.assertEqual(entry[key], template[key])
        self.assertEqual(entry["tool_mode"], "direct")
        self.assertIs(entry["use_responses_lite"], False)

    def test_a_window_that_is_not_a_positive_integer_is_refused(self):
        for window in (0, -1, True, "32768", 1.5, None):
            with self.subTest(window=window), self.assertRaises(ValueError):
                codex_catalog.build_local_entry(self.profile.model, window)

    def test_an_unknown_placeholder_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            codex_catalog.fill_template({"slug": "{{slug}}", "x": "{{surprise}}"},
                                        {"{{slug}}": "m"})
        self.assertIn("surprise", str(caught.exception))


class WriteCatalog(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.entry = codex_catalog.build_local_entry(sp.discovered_profile().model, 32768)

    def test_the_file_holds_one_model_and_the_hash_is_of_its_bytes(self):
        path = self.root / codex_catalog.CATALOG_NAME
        digest = codex_catalog.write_catalog(path, self.entry)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"models": [self.entry]})
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(digest, sp.lw.job_file_sha256(path))

    def test_the_same_entry_writes_the_same_bytes(self):
        first = codex_catalog.write_catalog(self.root / "a.json", self.entry)
        second = codex_catalog.write_catalog(self.root / "b.json", dict(reversed(
            list(self.entry.items()))))
        self.assertEqual(first, second)

    def test_a_missing_file_has_no_hash(self):
        self.assertIsNone(sp.lw.job_file_sha256(self.root / "absent.json"))

    def test_the_launcher_reads_the_catalog_through_the_one_safe_helper_only(self):
        self.assertFalse(hasattr(codex_catalog, "file_sha256"))  # no second, link-following reader


class Template(unittest.TestCase):
    def setUp(self):
        self.instructions = json.loads(codex_catalog.TEMPLATE_PATH.read_text(
            encoding="utf-8"))["base_instructions"]

    def test_hand_edits_go_through_the_patch_heredoc(self):
        self.assertIn("apply_patch <<'EOF'", self.instructions)
        self.assertIn("sed -i", self.instructions)

    def test_project_tools_and_moves_are_allowed(self):
        self.assertIn("formatters", self.instructions)
        self.assertIn("git mv", self.instructions)
        self.assertIn("*** Move to: ", self.instructions)

    def test_a_required_final_answer_format_wins_over_the_summary(self):
        self.assertIn("unless the task specifies the format of the final answer",
                      self.instructions)


if __name__ == "__main__":
    unittest.main()
