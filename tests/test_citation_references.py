"""Reference resolution keeps model-authored citation fields out of the trust path."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import read_answers
import source_citations


class CitationReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="citation-refs-")
        self.root = Path(self.temporary_directory.name) / "repo"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "outside").mkdir()
        self.source = self.root / "pkg" / "sample.py"
        self.source.write_text("first = 1\n\nsecond = 2\nthird = 3\n", encoding="utf-8")
        (self.root / "outside" / "other.py").write_text("secret = 1\n", encoding="utf-8")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def helper_payload(self, path="pkg/sample.py", start=1, count=40, scopes=None):
        return source_citations.build_payload(str(self.root), scopes or ["pkg"], [path], start, count)

    def envelope(self, citations):
        return {"answer": {"summary": "answer"}, "citations": citations}

    def resolve(self, envelope, outputs, scopes=None):
        return read_answers.resolve_citation_references(
            envelope, outputs, self.root, scopes or ["pkg"])

    def test_helper_emits_deterministic_references_for_nonblank_lines(self):
        payload = self.helper_payload(start=2, count=3)
        refs = payload["files"][0]["source_refs"]
        self.assertEqual([item["source_id"] for item in refs], [
            "source:pkg/sample.py:3-3", "source:pkg/sample.py:4-4",
        ])
        self.assertEqual([item["citation"] for item in refs],
                         payload["files"][0]["citations"])

    def test_observed_helper_reference_resolves_and_is_verified(self):
        payload = self.helper_payload(start=3, count=1)
        ref = payload["files"][0]["source_refs"][0]
        normalized, errors = self.resolve(self.envelope([{"source_id": ref["source_id"]}]),
                                          [json.dumps(payload)])
        self.assertEqual(errors, [])
        self.assertEqual(normalized["citations"], [ref["citation"]])
        self.assertEqual(read_answers.verify_citations(self.root, normalized["citations"])["verified"], 1)

    def test_reference_schema_accepts_source_ids_only(self):
        schema = read_answers.reference_envelope_schema({"type": "string"})
        valid = {"answer": "ok", "citations": [{"source_id": "source:pkg/a.py:1-1"}]}
        self.assertEqual(read_answers.validate(valid, schema), [])
        self.assertTrue(read_answers.validate(self.envelope([{
            "source_id": "source:pkg/a.py:1-1", "quote": "model text",
        }]), schema))

    def test_legacy_full_citations_are_left_unchanged(self):
        citation = {"path": "pkg/sample.py", "line_start": 3,
                    "line_end": 3, "quote": "second = 2"}
        original = self.envelope([citation])
        normalized, errors = self.resolve(original, [])
        self.assertEqual(errors, [])
        self.assertEqual(normalized, original)
        self.assertIsNot(normalized, original)

    def test_forged_or_unknown_source_ids_are_rejected(self):
        payload = self.helper_payload()
        actual = payload["files"][0]["source_refs"][0]["source_id"]
        for source_id, outputs in ((actual + "-forged", [json.dumps(payload)]),
                                   ("source:pkg/no.py:1-1", [])):
            with self.subTest(source_id=source_id):
                normalized, errors = self.resolve(self.envelope([{"source_id": source_id}]), outputs)
                self.assertTrue(errors)
                self.assertEqual(normalized["citations"], [{"source_id": source_id}])

    def test_malformed_reference_objects_are_rejected(self):
        for reference in ({"source_id": 3}, {"source_id": "source:pkg/sample.py:1-1", "quote": "x"}):
            with self.subTest(reference=reference):
                normalized, errors = self.resolve(self.envelope([reference]), [])
                self.assertTrue(errors)
                self.assertEqual(normalized["citations"], [reference])

    def test_bad_helper_quote_and_out_of_scope_citation_are_rejected(self):
        payload = self.helper_payload(start=3, count=1)
        ref = payload["files"][0]["source_refs"][0]
        bad_quote = copy.deepcopy(payload)
        bad_quote["files"][0]["citations"][0]["quote"] = "invented"
        bad_quote["files"][0]["source_refs"][0]["citation"]["quote"] = "invented"
        normalized, errors = self.resolve(self.envelope([{"source_id": ref["source_id"]}]),
                                          [json.dumps(bad_quote)])
        self.assertTrue(errors)
        self.assertEqual(normalized["citations"], [{"source_id": ref["source_id"]}])

        outside = source_citations.build_payload(str(self.root), ["outside"],
                                                 ["outside/other.py"])
        out_ref = outside["files"][0]["source_refs"][0]
        normalized, errors = self.resolve(self.envelope([{"source_id": out_ref["source_id"]}]),
                                          [json.dumps(outside)])
        self.assertTrue(errors)
        self.assertEqual(normalized["citations"], [{"source_id": out_ref["source_id"]}])

    def test_identical_repeated_helper_ids_resolve_to_the_same_citation(self):
        payload = self.helper_payload(start=3, count=1)
        source_id = payload["files"][0]["source_refs"][0]["source_id"]
        normalized, errors = self.resolve(self.envelope([{"source_id": source_id}]),
                                          [json.dumps(payload), json.dumps(payload)])
        self.assertEqual(errors, [])
        self.assertEqual(normalized["citations"], [payload["files"][0]["citations"][0]])

    def test_conflicting_repeated_helper_ids_are_ambiguous(self):
        first = self.helper_payload(start=3, count=1)
        second = copy.deepcopy(first)
        second["files"][0]["citations"][0]["quote"] = "second"
        second["files"][0]["source_refs"][0]["citation"]["quote"] = "second"
        source_id = first["files"][0]["source_refs"][0]["source_id"]
        normalized, errors = self.resolve(self.envelope([{"source_id": source_id}]),
                                          [json.dumps(first), json.dumps(second)])
        self.assertTrue(any("ambiguous" in error for error in errors))
        self.assertEqual(normalized["citations"], [{"source_id": source_id}])

    def test_helper_outputs_are_bounded_before_parsing(self):
        with mock.patch.object(read_answers, "MAX_REFERENCE_OUTPUT_BYTES", 10), \
                mock.patch.object(read_answers.json, "loads", wraps=json.loads) as loads:
            normalized, errors = self.resolve(self.envelope([{"source_id": "source:pkg/sample.py:3-3"}]),
                                              ["{" + (" " * 20)])
            loads.assert_not_called()
        self.assertTrue(any("output limit" in error for error in errors))
        self.assertEqual(normalized["citations"], [{"source_id": "source:pkg/sample.py:3-3"}])

        with mock.patch.object(read_answers, "MAX_REFERENCE_OUTPUTS", 0):
            normalized, errors = self.resolve(
                self.envelope([{"source_id": "source:pkg/sample.py:3-3"}]), ["{}"])
        self.assertTrue(any("count exceeds" in error for error in errors))
        self.assertEqual(normalized["citations"], [{"source_id": "source:pkg/sample.py:3-3"}])

    def test_non_json_helper_output_is_rejected_without_leaking_contents(self):
        normalized, errors = self.resolve(self.envelope([{"source_id": "source:pkg/sample.py:3-3"}]),
                                          ["private source contents"])
        self.assertTrue(errors)
        self.assertNotIn("private source contents", " ".join(errors))
        self.assertEqual(normalized["citations"], [{"source_id": "source:pkg/sample.py:3-3"}])

    def test_unrelated_json_files_key_does_not_poison_helper_references(self):
        payload = self.helper_payload(start=3, count=1)
        source_id = payload["files"][0]["source_refs"][0]["source_id"]
        normalized, errors = self.resolve(self.envelope([{"source_id": source_id}]),
            [json.dumps({"files": ["manifest.py"]}), json.dumps(payload)])
        self.assertEqual(errors, [])
        self.assertEqual(normalized["citations"][0]["path"], "pkg/sample.py")

    def test_unrelated_command_output_does_not_break_a_valid_reference(self):
        payload = self.helper_payload(start=3, count=1)
        ref = payload["files"][0]["source_refs"][0]
        normalized, errors = self.resolve(self.envelope([{"source_id": ref["source_id"]}]),
                                          ["ls output", json.dumps(payload)])
        self.assertEqual(errors, [])
        self.assertEqual(normalized["citations"], [ref["citation"]])


if __name__ == "__main__":
    unittest.main()
