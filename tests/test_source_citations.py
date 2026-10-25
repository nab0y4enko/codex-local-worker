"""Bounded, scoped citation source reads for read workers."""
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import read_answers
import source_citations


HELPER = Path(__file__).resolve().parents[1] / "src" / "source_citations.py"


class SourceCitationCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-citations-")
        self.root = Path(self.temp.name) / "repo"
        (self.root / "pkg").mkdir(parents=True)
        self.source = self.root / "pkg" / "sample.py"
        self.source.write_text("first = 1\n\nsecond = 2\nthird = 3\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, *arguments):
        return subprocess.run([sys.executable, str(HELPER), "--repo", str(self.root),
                               "--scope", "pkg", *arguments], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_numbered_output_keeps_blank_lines_and_emits_verifiable_citations(self):
        result = self.run_helper("--path", "pkg/sample.py", "--start", "2", "--count", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        file_result = payload["files"][0]
        self.assertEqual(file_result["lines"], [
            {"number": 2, "text": ""},
            {"number": 3, "text": "second = 2"},
            {"number": 4, "text": "third = 3"},
        ])
        self.assertEqual(file_result["citations"], [
            {"path": "pkg/sample.py", "line_start": 3, "line_end": 3,
             "quote": "second = 2"},
            {"path": "pkg/sample.py", "line_start": 4, "line_end": 4,
             "quote": "third = 3"},
        ])
        self.assertEqual(file_result["source_refs"], [
            {"source_id": "source:pkg/sample.py:3-3",
             "citation": file_result["citations"][0]},
            {"source_id": "source:pkg/sample.py:4-4",
             "citation": file_result["citations"][1]},
        ])
        self.assertEqual(read_answers.verify_citations(self.root, file_result["citations"]),
                         {"total": 2, "verified": 2, "failures": []})

    def test_ready_quote_still_fails_when_cited_to_the_wrong_in_range_line(self):
        result = self.run_helper("--path", "pkg/sample.py", "--start", "3", "--count", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        generated = payload["files"][0]["citations"][0]
        wrong_line = dict(generated, line_start=4, line_end=4)
        checked = read_answers.verify_citations(self.root, [wrong_line])
        self.assertEqual((checked["verified"], len(checked["failures"])), (0, 1))

    def test_path_outside_declared_scope_is_rejected(self):
        (self.root / "outside.py").write_text("secret = 1\n", encoding="utf-8")
        result = self.run_helper("--path", "outside.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("outside the declared scope", result.stderr)

    def test_parent_traversal_is_rejected(self):
        result = self.run_helper("--path", "../outside.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("relative", result.stderr)

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("secret = 1\n", encoding="utf-8")
        (self.root / "pkg" / "escape.py").symlink_to(outside)
        result = self.run_helper("--path", "pkg/escape.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the repository", result.stderr)

    def test_symlink_outside_scope_but_inside_repository_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (other / "source.py").write_text("secret = 1\n", encoding="utf-8")
        (self.root / "pkg" / "linked.py").symlink_to(other / "source.py")
        result = self.run_helper("--path", "pkg/linked.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("outside the declared scope", result.stderr)

    def test_absolute_paths_are_rejected(self):
        result = self.run_helper("--path", str(self.source))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree-relative", result.stderr)

    def test_count_above_limit_is_rejected_without_partial_output(self):
        result = self.run_helper("--path", "pkg/sample.py", "--count", "81")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("count", result.stderr.lower())

    def test_source_file_above_read_limit_is_rejected(self):
        (self.root / "pkg" / "huge.py").write_text("x" * (2 * 1024 * 1024 + 1),
                                                       encoding="utf-8")
        result = self.run_helper("--path", "pkg/huge.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("source read limit", result.stderr)

    def test_output_limit_includes_the_trailing_newline(self):
        payload = source_citations.build_payload(str(self.root), ["pkg"], ["pkg/sample.py"])
        encoded_size = len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        final_size = encoded_size + 1
        with mock.patch.object(source_citations, "MAX_OUTPUT_BYTES", final_size - 1):
            with self.assertRaisesRegex(ValueError, "output limit"):
                source_citations.build_payload(str(self.root), ["pkg"], ["pkg/sample.py"])

    def test_excessive_scope_count_is_rejected_before_scope_resolution(self):
        with mock.patch.object(source_citations, "_scopes") as resolver:
            with self.assertRaisesRegex(ValueError, "at most 32 scopes"):
                source_citations.build_payload(str(self.root), ["pkg"] * 33,
                                               ["pkg/sample.py"])
        resolver.assert_not_called()

    def test_excessive_path_count_is_rejected_before_scope_resolution(self):
        with mock.patch.object(source_citations, "_scopes") as resolver:
            with self.assertRaisesRegex(ValueError, "at most 16 paths"):
                source_citations.build_payload(str(self.root), ["pkg"],
                                               ["pkg/sample.py"] * 17)
        resolver.assert_not_called()

    def test_scope_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "source.py").write_text("secret = 1\n", encoding="utf-8")
        (self.root / "escape").symlink_to(outside)
        result = subprocess.run([sys.executable, str(HELPER), "--repo", str(self.root),
                                 "--scope", "escape", "--path", "escape/source.py"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("scope resolves outside the repository", result.stderr)

    def test_oversized_selected_output_is_rejected_without_truncation(self):
        (self.root / "pkg" / "large.py").write_text("x = '" + ("x" * 70000) + "'\n",
                                                       encoding="utf-8")
        result = self.run_helper("--path", "pkg/large.py", "--count", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("output limit", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
