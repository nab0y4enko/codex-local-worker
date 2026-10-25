"""The doctor reports usable diagnostics without writing setup or permission state."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import local_worker as lw
import permissions
import support as sp


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="local-worker-doctor-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.temp), ignore_errors=True))
        self.settings = sp.write_settings(self.temp / "omlx" / "settings.json")
        self.state = self.temp / "state"
        self.codex = self.temp / "codex"

    def fake_codex(self, argv, **kwargs):
        output = "codex-cli 0.164.0" if argv[-1] == "--version" else "\n".join(lw.CODEX_REQUIRED_FLAGS)
        return subprocess.CompletedProcess(argv, 0, output, "")

    def fake_simctl(self, argv):
        payload = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
            {"udid": "11111111-2222-3333-4444-555555555555", "name": "iPhone Example",
             "state": "Booted"}]}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    def report(self, **kwargs):
        args = {"settings_path": self.settings, "state_dir": self.state,
                "codex_bin": str(self.codex), "codex_run": self.fake_codex,
                "omlx_fetch": sp.FakeOmlx("one"), "simctl_run": self.fake_simctl}
        args.update(kwargs)
        return lw.doctor_report(**args)

    def test_doctor_reports_every_section_without_creating_permission_state(self):
        out = self.report()
        self.assertEqual(sorted(out), ["codex", "omlx", "permissions", "plugin_version",
                                       "simulators", "state_dir"])
        self.assertEqual(out["codex"], {"version": "0.164.0", "flags_ok": True})
        self.assertEqual(out["permissions"]["mode"], permissions.MODE_FULL_HOST)
        self.assertIsInstance(out["permissions"]["deny_list"], dict)
        self.assertIn("wrapper_available", out["permissions"])
        self.assertTrue(out["simulators"])
        self.assertFalse((self.state / permissions.PERMISSIONS_FILE).exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(out["plugin_version"], json.loads(
            (ROOT / ".codex-plugin/plugin.json").read_text())["version"])

    def test_doctor_reports_discovery_errors_by_code(self):
        out = self.report(omlx_fetch=sp.FakeOmlx("none_loaded"))
        self.assertIn("error_code", out["omlx"])
        self.assertEqual(out["omlx"]["error_code"], "model_not_loaded")

    def test_doctor_reads_existing_permissions_without_changing_them(self):
        self.state.mkdir()
        path = self.state / permissions.PERMISSIONS_FILE
        path.write_text(permissions._dump(permissions.default_permissions()), encoding="utf-8")
        before = path.read_bytes()
        out = self.report()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(out["permissions"]["deny_list"]["read"],
                         permissions.default_permissions().deny_read)

    def test_doctor_cli_emits_the_json_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = lw.main(["doctor", "--json", "--settings", str(self.settings),
                            "--state-dir", str(self.state), "--codex-bin", str(self.codex)],
                           omlx_fetch=sp.FakeOmlx("one"), codex_run=self.fake_codex,
                           simctl_run=self.fake_simctl)
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(sorted(report), ["codex", "omlx", "permissions", "plugin_version",
                                          "simulators", "state_dir"])
        self.assertFalse((self.state / permissions.PERMISSIONS_FILE).exists())


if __name__ == "__main__":
    unittest.main()
