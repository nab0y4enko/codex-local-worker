import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import src.test_review as scanner
from src.test_review import review_python_tests


def _write(root: Path, name: str, source: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _codes(root: Path, paths):
    return [(item["path"], item["test"], item["code"]) for item in review_python_tests(root, paths)]


class TestReviewPythonTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_flags_test_that_computes_expected_result_without_assertion(self):
        _write(self.root, "tests/test_result.py", "def test_result():\n    result = calculate()\n    expected = 3\n")

        findings = review_python_tests(self.root, ["tests/test_result.py"])

        self.assertEqual([(item["test"], item["code"]) for item in findings], [("test_result", "missing_assertion")])
        self.assertEqual(findings[0]["path"], "tests/test_result.py")
        self.assertEqual(findings[0]["line"], 1)
        self.assertIn("advisory", findings[0]["message"].lower())

    def test_flags_duplicate_literal_dictionary_keys_even_with_assertion(self):
        _write(self.root, "tests/test_counts.py", "def test_counts():\n    assert counts() == {'word': 1, 'word': 2, True: 3, 1: 4}\n")
        findings = review_python_tests(self.root, ["tests/test_counts.py"])
        self.assertEqual([f["code"] for f in findings], ["duplicate_dictionary_key", "duplicate_dictionary_key"])
        self.assertTrue(all(f["line"] == 2 for f in findings))

    def test_dynamic_dictionary_keys_are_not_evaluated(self):
        _write(self.root, "tests/test_counts.py", "def test_counts():\n    assert counts() == {key(): 1, other(): 2}\n")
        self.assertEqual(review_python_tests(self.root, ["tests/test_counts.py"]), [])

    def test_accepts_direct_assertions_and_unittest_assertions(self):
        _write(self.root, "tests/test_result.py", """import unittest
def test_plain():
    assert calculate() == 3
class_placeholder = 0
class Example(unittest.TestCase):
    def test_method(self):
        self.assertEqual(calculate(), 3)
""")

        self.assertEqual(_codes(self.root, ["tests/test_result.py"]), [])

    def test_nested_helper_assertion_does_not_satisfy_test(self):
        _write(self.root, "tests/test_nested.py", """def test_nested():
    def check():
        assert calculate() == 3
    check()
""")

        self.assertEqual(
            _codes(self.root, ["tests/test_nested.py"]),
            [("tests/test_nested.py", "test_nested", "missing_assertion")],
        )

    def test_expected_exception_context_counts_as_assertion(self):
        contexts = ("pytest.raises(ValueError)", "self.assertRaises(ValueError)")
        for context in contexts:
            with self.subTest(context=context):
                _write(self.root, "tests/test_exception.py", """import pytest
import unittest
class Example(unittest.TestCase):
    def test_exception(self):
        with CONTEXT:
            fail()
""".replace("CONTEXT", context))

                self.assertEqual(_codes(self.root, ["tests/test_exception.py"]), [])

    def test_skips_decorated_skip_and_unrelated_changed_paths(self):
        _write(self.root, "tests/test_skip.py", """import pytest
@pytest.mark.skip(reason="later")
def test_skipped():
    pass
def test_active():
    pass
""")
        _write(self.root, "src/ordinary.py", "def test_missing():\n    pass\n")

        self.assertEqual(
            _codes(self.root, ["tests/test_skip.py", "src/ordinary.py", "tests/test_skip.py.md"]),
            [("tests/test_skip.py", "test_active", "missing_assertion")],
        )

    def test_rejects_path_outside_worktree_and_symlink_escape(self):
        outside = self.root.parent / ("test_" + self.root.name + "_outside.py")
        outside.write_text("def test_hidden():\n    pass\n", encoding="utf-8")
        link = self.root / "tests" / "test_link.py"
        link.parent.mkdir()
        link.symlink_to(outside)
        try:
            findings = review_python_tests(self.root, [str(outside), "tests/test_link.py"])
            self.assertEqual([(item["path"], item["code"]) for item in findings], [
                (str(outside), "unreadable_test"),
                ("tests/test_link.py", "unreadable_test"),
            ])
        finally:
            outside.unlink()

    def test_reports_syntax_errors_as_unreadable_test(self):
        _write(self.root, "tests/test_broken.py", "def test_broken(:\n")

        findings = review_python_tests(self.root, ["tests/test_broken.py"])

        self.assertEqual(findings[0]["code"], "unreadable_test")
        self.assertEqual(findings[0]["line"], 1)

    def test_rejects_test_file_over_size_limit(self):
        _write(self.root, "tests/test_large.py", "#" + ("x" * 1_000_000))

        findings = review_python_tests(self.root, ["tests/test_large.py"])

        self.assertEqual(findings[0]["code"], "unreadable_test")

    def test_reads_source_with_a_fixed_byte_limit(self):
        _write(self.root, "tests/test_bounded.py", "def test_bounded():\n    pass\n")
        original_open = Path.open
        read_sizes = []

        class TrackingFile:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def read(self, size=-1):
                read_sizes.append(size)
                return self.wrapped.read(size)

        def tracked_open(path, *args, **kwargs):
            opened = original_open(path, *args, **kwargs)
            return TrackingFile(opened) if path.name == "test_bounded.py" else opened

        with mock.patch.object(Path, "open", tracked_open):
            review_python_tests(self.root, ["tests/test_bounded.py"])

        self.assertEqual(read_sizes, [scanner._MAX_FILE_BYTES + 1])

    def test_ast_recursion_error_becomes_safe_advisory_finding(self):
        _write(self.root, "tests/test_recursive.py", "def test_recursive():\n    pass\n")

        def fail_parse(*args, **kwargs):
            raise RecursionError("source-controlled detail")

        with mock.patch.object(scanner, "ast", SimpleNamespace(parse=fail_parse)):
            findings = review_python_tests(self.root, ["tests/test_recursive.py"])

        self.assertEqual(findings[0]["code"], "unreadable_test")
        self.assertIn("RecursionError", findings[0]["message"])
        self.assertNotIn("source-controlled detail", findings[0]["message"])

    def test_parser_error_details_are_not_disclosed(self):
        _write(self.root, "tests/test_parser.py", "def test_parser(:\n")

        def fail_parse(*args, **kwargs):
            raise SyntaxError("secret source fragment")

        with mock.patch.object(scanner, "ast", SimpleNamespace(parse=fail_parse)):
            findings = review_python_tests(self.root, ["tests/test_parser.py"])

        self.assertEqual(findings[0]["code"], "unreadable_test")
        self.assertIn("SyntaxError", findings[0]["message"])
        self.assertNotIn("secret source fragment", findings[0]["message"])

    def test_cli_emits_json_and_returns_zero_for_advisories(self):
        _write(self.root, "tests/test_cli.py", "def test_cli():\n    result = 1\n    expected = 1\n")

        result = subprocess.run(
            [sys.executable, "-m", "src.test_review", "--repo", str(self.root), "--path", "tests/test_cli.py"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)[0]["code"], "missing_assertion")

    def test_cli_rejects_missing_repository(self):
        result = subprocess.run(
            [sys.executable, "-m", "src.test_review", "--repo", str(self.root / "missing"), "--path", "tests/test.py"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
