"""Contract the generated native pilot to separate readiness from its real assertion."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "prepare_native_live_pilot.py"
TEST_TARGET = "NativePilotUITests/NativePilotUITests"


class NativeLivePilotTests(unittest.TestCase):
    def test_plan_captures_readiness_while_contract_selects_expected_after_assertion(self):
        with tempfile.TemporaryDirectory(prefix="native pilot contract ") as temp:
            fixture_root = Path(temp) / "fixture with spaces"
            subprocess.run([sys.executable, str(HELPER), "--root", str(fixture_root),
                            "--simulator-udid", "24033911-49F7-4D8F-ADDD-233ECB533393"],
                           cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=60)
            task = json.loads((fixture_root / "task.json").read_text(encoding="utf-8"))
            plan = json.loads((fixture_root / "native-plan.json").read_text(encoding="utf-8"))
            test_source = (fixture_root / "repo" / "NativePilotUITests.swift").read_text(encoding="utf-8")

        task_command = task["test_commands"][0]
        readiness_command = plan["test"]
        task_selector = "-only-testing:" + TEST_TARGET + "/testAppShowsAfter"
        readiness_selector = "-only-testing:" + TEST_TARGET + "/testAppShowsQualificationLabel"
        self.assertLess(task_command.index("test"), task_command.index(task_selector))
        self.assertLess(readiness_command.index("test"), readiness_command.index(readiness_selector))
        self.assertNotIn("testAppShowsAfter", readiness_command)
        self.assertNotIn("testAppShowsQualificationLabel", task_command)
        self.assertIn('XCTAssertEqual(label.label, "After")', test_source)
        self.assertIn("func testAppShowsAfter()", test_source)
        self.assertIn("func testAppShowsQualificationLabel()", test_source)


if __name__ == "__main__":
    unittest.main()
