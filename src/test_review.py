"""Advisory checks for changed Python test files."""

import argparse
import ast
import json
import os
from pathlib import Path
from typing import Dict, List, Sequence


_MAX_FILE_BYTES = 1_000_000
_MAX_AST_NODES = 100_000
_FINDING_KEYS = ("path", "line", "test", "code", "message")


def _is_python_test(path: str) -> bool:
    candidate = Path(path)
    return candidate.suffix == ".py" and (candidate.name.startswith("test_") or candidate.name.endswith("_test.py"))


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return (prefix + "." if prefix else "") + node.attr
    return ""


def _is_skipped(function: ast.FunctionDef) -> bool:
    for decorator in function.decorator_list:
        name = _dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if name.endswith(".skip") or name.endswith(".skipif"):
            return True
    return False


def _is_test_case_class(node: ast.ClassDef) -> bool:
    return any(_dotted_name(base).split(".")[-1] == "TestCase" for base in node.bases)


def _contains_assertion(statements: Sequence[ast.stmt]) -> bool:
    """Inspect a test's own statements while excluding nested scopes."""
    pending = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            leaf = name.rsplit(".", 1)[-1]
            is_self_assert = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and leaf.startswith("assert")
            )
            if is_self_assert or name.endswith(".fail") or name == "fail":
                return True
        if isinstance(node, ast.With):
            for item in node.items:
                name = _dotted_name(item.context_expr.func) if isinstance(item.context_expr, ast.Call) else ""
                if name.endswith("assertRaises") or name.endswith("pytest.raises"):
                    return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _finding(path: str, line: int, test: str, code: str, message: str) -> Dict[str, object]:
    return dict(zip(_FINDING_KEYS, (path, line, test, code, message)))


def _test_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node
        elif isinstance(node, ast.ClassDef) and _is_test_case_class(node):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                    yield method


def review_python_tests(worktree: Path, changed_paths: Sequence[str]) -> List[Dict[str, object]]:
    """Return advisory findings for changed Python test files without importing them."""
    root = Path(worktree).resolve()
    findings = []
    for raw_path in changed_paths:
        supplied = os.fspath(raw_path)
        if not _is_python_test(supplied):
            continue
        candidate = Path(supplied)
        lexical = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = lexical.resolve(strict=True)
            display = str(candidate.relative_to(root)) if candidate.is_absolute() and candidate.is_relative_to(root) else (
                str(candidate) if candidate.is_absolute() else candidate.as_posix()
            )
        except (OSError, ValueError):
            display = supplied
            findings.append(_finding(display, 1, "", "unreadable_test", "Advisory: test file cannot be resolved safely."))
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            findings.append(_finding(display, 1, "", "unreadable_test", "Advisory: test file resolves outside the worktree."))
            continue
        try:
            if not resolved.is_file():
                raise ValueError("not a regular file")
            with resolved.open("rb") as source_file:
                source_bytes = source_file.read(_MAX_FILE_BYTES + 1)
            if len(source_bytes) > _MAX_FILE_BYTES:
                raise ValueError("file size limit exceeded")
            source = source_bytes.decode("utf-8")
            tree = ast.parse(source, filename=display)
            if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
                raise ValueError("syntax tree limit exceeded")
            file_findings = []
            for function in _test_functions(tree):
                if _is_skipped(function) or _contains_assertion(function.body):
                    continue
                file_findings.append(_finding(
                    display,
                    function.lineno,
                    function.name,
                    "missing_assertion",
                    "Advisory: this test has no direct assertion, failure call, or expected-exception context; helper-based checks may still be valid.",
                ))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as error:
            line = getattr(error, "lineno", None) or 1
            findings.append(_finding(
                display, line, "", "unreadable_test",
                "Advisory: test file could not be reviewed safely ({}).".format(type(error).__name__),
            ))
            continue
        findings.extend(file_findings)
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Advisory scanner for changed Python tests")
    parser.add_argument("--repo", required=True, type=Path, help="worktree root")
    parser.add_argument("--path", action="append", required=True, help="changed test path (repeatable)")
    args = parser.parse_args(argv)
    if not args.repo.is_dir():
        parser.error("repository path must be an existing directory")
    print(json.dumps(review_python_tests(args.repo, args.path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
