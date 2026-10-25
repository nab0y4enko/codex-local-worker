"""Screenshot decoding, safe overlays, capture reporting and job integration."""
import sys
import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import screenshots


class CaptureJobRunner:
    def __init__(self, frames="mixed", worker_edit=None):
        self.frames = frames
        self.delegate = sp.FakeRun(worker_edit=worker_edit or sp.fix_add)
        self.udid = None
        self.calls = []

    def __call__(self, argv, **kwargs):
        raw = [str(arg) for arg in argv]
        inner, wrapped = sp.deny_wrapped(raw)
        self.calls.append(inner)
        if inner and Path(inner[0]).name == "xcodebuild":
            if "build" in inner and "test" not in inner:
                product_dir = Path(next(arg.split("=", 1)[1] for arg in inner
                                        if arg.startswith("CONFIGURATION_BUILD_DIR=")))
                app = product_dir / "App.app"
                app.mkdir(parents=True, exist_ok=True)
                (app / "Info.plist").write_bytes(plistlib.dumps(
                    {"CFBundleIdentifier": "org.example.App"}))
            elif inner[-1] == "test":
                Path(inner[inner.index("-resultBundlePath") + 1]).mkdir(parents=True)
                destination = inner[inner.index("-destination") + 1]
                self.udid = destination.split("=", 1)[1]
            return sp.result(raw, 0)
        if inner and Path(inner[0]).name == "xcrun":
            if inner[1:3] == ["xcresulttool", "get"]:
                summary = {"result": "Passed", "totalTestCount": 1, "passedTests": 1,
                           "failedTests": 0, "skippedTests": 0,
                           "devicesAndConfigurations": [{"device": {"deviceId": self.udid},
                                                          "passedTests": 1, "failedTests": 0,
                                                          "skippedTests": 0}]}
                return sp.result(raw, 0, json.dumps(summary))
            if inner[1:3] == ["simctl", "launch"]:
                return sp.result(raw, 0, "org.example.App: 123\n")
            if inner[1:3] == ["simctl", "io"] and inner[3] == self.udid:
                dot = None if self.frames == "black" else (255, 255, 255)
                sp.write_png(inner[-1], 4, 4, (0, 0, 0), dot=dot)
                return sp.result(raw, 0)
            return sp.result(raw, 0)
        return self.delegate(raw, **kwargs)


class PngTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_uniform_png_is_detected(self):
        black = sp.write_png(self.root / "black.png", 4, 4, (0, 0, 0))
        mixed = sp.write_png(self.root / "mixed.png", 4, 4, (0, 0, 0),
                             dot=(255, 255, 255))
        self.assertTrue(screenshots.png_is_uniform(black))
        self.assertFalse(screenshots.png_is_uniform(mixed))

    def test_malformed_and_unsupported_pngs_are_reported_as_errors(self):
        broken = self.root / "broken.png"
        broken.write_bytes(b"not png")
        with self.assertRaises(ValueError):
            screenshots.png_is_uniform(broken)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.after = self.root / "after"
        self.before = self.root / "before"
        self.before.mkdir()
        (self.after / "UITests").mkdir(parents=True)
        (self.after / "App").mkdir(parents=True)
        self.after.joinpath("UITests/Settings.swift").write_text("test harness")
        self.after.joinpath("App/Main.swift").write_text("implementation")

    def test_overlay_only_copies_declared_harness_paths(self):
        copied = screenshots.apply_overlay(self.after, self.before,
                                           ["UITests/Settings.swift", "App/Main.swift"],
                                           allowed_roots=["UITests"])
        self.assertEqual(copied, ["UITests/Settings.swift"])
        self.assertEqual((self.before / "UITests/Settings.swift").read_text(), "test harness")
        self.assertFalse((self.before / "App/Main.swift").exists())

    def test_overlay_rejects_paths_that_escape_harness_roots(self):
        with self.assertRaises(ValueError):
            screenshots.apply_overlay(self.after, self.before, ["../App/Main.swift"],
                                       allowed_roots=["UITests"])

    def test_overlay_rejects_symlinked_source_file_and_destination_parent(self):
        outside = self.root / "outside.swift"
        outside.write_text("secret")
        self.after.joinpath("UITests/Linked.swift").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "source contains a symlink"):
            screenshots.apply_overlay(self.after, self.before, ["UITests/Linked.swift"],
                                       allowed_roots=["UITests"])
        (self.before / "UITests").mkdir(parents=True)
        shutil.rmtree(self.before / "UITests")
        (self.before / "UITests").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "target contains a symlink"):
            screenshots.apply_overlay(self.after, self.before, ["UITests/Settings.swift"],
                                       allowed_roots=["UITests"])

    def test_overlay_rejects_symlinked_ancestor_root(self):
        alias = self.root / "alias"
        alias.symlink_to(self.after, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "source root must not be a symlink"):
            screenshots.apply_overlay(alias, self.before, ["UITests/Settings.swift"],
                                       allowed_roots=["UITests"])


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "worktree"
        self.work.mkdir()
        self.out = self.root / "report" / "screenshots"
        self.udid = "12345678-1234-1234-1234-123456789ABC"
        self.plan = {
            "version": 2, "destination": {"simulator": self.udid},
            "bundle_id": "org.example.App", "app_bundle": "build/App.app",
            "result_bundle": "results.xcresult",
            "build": ["xcodebuild", "-project", "{worktree}/App.xcodeproj",
                      "-scheme", "App", "-destination", "id={simulator_udid}", "build"],
            "install": ["xcrun", "simctl", "install", "{simulator_udid}", "{app_bundle}"],
            "launch": ["xcrun", "simctl", "launch", "{simulator_udid}", "{bundle_id}"],
            "test": ["xcodebuild", "-project", "{worktree}/App.xcodeproj", "-scheme", "App",
                     "-destination", "id={simulator_udid}", "-resultBundlePath",
                     "{result_bundle}", "test"],
            "screenshots": [{"name": "settings", "launch_arguments": ["--screen", "settings"],
                             "settle_seconds": 0}],
        }
        self.source = screenshots.CaptureSource("after", self.root, "result", "App",
                                                {"simulator": self.udid})

    def test_capture_source_builds_unique_artifacts_and_writes_screenshot(self):
        calls = []
        summary = {"result": "Passed", "totalTestCount": 1, "passedTests": 1,
                   "failedTests": 0, "skippedTests": 0,
                   "devicesAndConfigurations": [{"device": {"deviceId": self.udid},
                                                  "passedTests": 1, "failedTests": 0,
                                                  "skippedTests": 0}]}

        def run(argv):
            argv = [str(arg) for arg in argv]
            calls.append(argv)
            stdout = ""
            if argv[0].endswith("xcodebuild") and "build" in argv and "test" not in argv:
                product_dir = next(arg.split("=", 1)[1] for arg in argv
                                    if arg.startswith("CONFIGURATION_BUILD_DIR="))
                app = Path(product_dir) / "App.app"
                app.mkdir(parents=True)
                (app / "Info.plist").write_bytes(plistlib.dumps(
                    {"CFBundleIdentifier": "org.example.App"}))
            elif argv[1:3] == ["simctl", "launch"] and argv[2] != "launch":
                stdout = "org.example.App: 123\n"
            elif argv[1:3] == ["simctl", "launch"]:
                stdout = "org.example.App: 123\n"
            elif argv[1:2] == ["xcresulttool"] and argv[2:4] == ["get", "test-results"]:
                stdout = json.dumps(summary)
            elif argv[-1] == "test":
                path = Path(argv[argv.index("-resultBundlePath") + 1])
                path.mkdir(parents=True)
            elif argv[1:3] == ["simctl", "io"]:
                sp.write_png(argv[-1], 4, 4, (1, 2, 3), dot=(4, 5, 6))
            return {"returncode": 0, "stdout": stdout, "stderr": ""}

        result = screenshots.capture_source(self.plan, self.source, self.work, self.out, run)
        self.assertEqual(result[0]["method"], "simctl")
        self.assertIsNone(result[0]["error"])
        self.assertFalse(result[0]["uniform"])
        self.assertTrue(Path(result[0]["path"]).is_file())
        self.assertTrue(any("CONFIGURATION_BUILD_DIR=" in arg for call in calls for arg in call))

    def test_capture_source_reports_malformed_png_as_an_error(self):
        original_uniform = screenshots.png_is_uniform
        with mock.patch.object(screenshots, "png_is_uniform",
                               side_effect=ValueError("unsupported PNG format")):
            runner = CaptureJobRunner()
            result = screenshots.capture_source(self.plan, self.source, self.work, self.out,
                                                lambda argv: runner(argv))
        self.assertEqual(result[0]["error"], "unsupported PNG format")
        self.assertFalse(result[0]["uniform"])

    def test_capture_only_job_with_a_blank_frame_fails_with_reason(self):
        repo = sp.make_repo(self.root)
        plan_path = self.root / "native-plan.json"
        plan = dict(self.plan)
        plan_path.write_text(json.dumps(plan))
        payload = {"task_id": "capture-1", "kind": "capture", "repo_path": str(repo),
                   "base_ref": "main", "goal": "Capture the current app",
                   "timeout_seconds": 120, "capture": {"native_plan": str(plan_path)}}
        task = sp.lw.Task.from_payload(payload)
        options = sp.job_options(self.root, repo)
        options.task = task
        runner = CaptureJobRunner(frames="black")
        with mock.patch.object(sp.lw, "run_bounded", runner):
            report = sp.lw.run_job(options)
        self.assertEqual(report["status"], "capture_failed")
        self.assertIn("uniform", report["failure_reason"])
        self.assertEqual(report["exit_code"], 13)
        self.assertEqual(runner.delegate.exec_calls, 0)

    def test_ui_review_captures_before_after_and_reference(self):
        repo = sp.make_repo(self.root)
        plan_path = self.root / "native-plan.json"
        plan = dict(self.plan, capture_sources=[{
            "name": "original", "repo_path": str(repo), "ref": "main",
            "scheme": "TiviaryOriginal", "destination": {"simulator": self.udid}}])
        plan_path.write_text(json.dumps(plan))
        options = sp.job_options(self.root, repo, tier={"local": True},
                                  native_plan=str(plan_path), ui_review=True)
        runner = CaptureJobRunner()
        with mock.patch.object(sp.lw, "run_bounded", runner), \
                mock.patch.object(sp.lw, "native_gate", lambda *args, **kwargs: None):
            report = sp.lw.run_job(options)
        self.assertEqual(sorted(report["screenshots"]["sources"]),
                         ["after", "before", "original"])
        index = Path(report["screenshots"]["index"]).read_text()
        self.assertIn("| settings |", index)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertTrue(report["primary_unchanged"])
        self.assertEqual(runner.delegate.exec_calls, 1)
        product_dirs = [arg.split("=", 1)[1] for argv in runner.calls
                        for arg in argv if arg.startswith("CONFIGURATION_BUILD_DIR=")]
        self.assertEqual(len(product_dirs), 3)
        self.assertEqual(len(set(product_dirs)), 3)

    def test_index_has_one_row_per_name_and_source_columns(self):
        before = sp.write_png(self.out / "before" / "settings.png", 2, 2, (1, 2, 3),
                              dot=(4, 5, 6))
        after = sp.write_png(self.out / "after" / "settings.png", 2, 2, (1, 2, 3),
                             dot=(6, 5, 4))
        index = screenshots.write_index(self.out, {
            "before": [{"name": "settings", "path": str(before), "error": None}],
            "after": [{"name": "settings", "path": str(after), "error": None}],
        })
        text = index.read_text()
        self.assertIn("| settings |", text)
        self.assertIn("| before | after |", text)


if __name__ == "__main__":
    unittest.main()
