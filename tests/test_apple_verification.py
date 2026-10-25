"""Simulator metadata helpers retained for platform-generic native verification."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import apple_verification as apple  # noqa: E402


class SimulatorMetadataTests(unittest.TestCase):
    def test_validate_simulator_udid_accepts_bare_uuid_only(self):
        self.assertEqual(apple.validate_simulator_udid(" 12345678-1234-1234-1234-123456789ABC "),
                         "12345678-1234-1234-1234-123456789ABC")
        for value in ("", "not-a-uuid", "id=12345678-1234-1234-1234-123456789ABC", None):
            with self.subTest(value=value), self.assertRaises(apple.ApplePolicyError):
                apple.validate_simulator_udid(value)

    def test_simctl_devices_keeps_runtime_for_each_device(self):
        listing = {"devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
                {"udid": "12345678-1234-1234-1234-123456789ABC", "isAvailable": True}],
            "com.apple.CoreSimulator.SimRuntime.tvOS-27-0": [
                {"udid": "87654321-4321-4321-4321-CBA987654321", "isAvailable": True}],
        }}
        calls = []

        def run(argv):
            calls.append(argv)
            return {"returncode": 0, "stdout": json.dumps(listing), "stderr": ""}

        devices = apple.simctl_devices(run)
        self.assertEqual(calls, [["/usr/bin/xcrun", "simctl", "list", "devices",
                                  "available", "--json"]])
        self.assertEqual(devices["12345678-1234-1234-1234-123456789ABC"]["runtime"],
                         "com.apple.CoreSimulator.SimRuntime.iOS-27-0")
        self.assertEqual(devices["87654321-4321-4321-4321-CBA987654321"]["runtime"],
                         "com.apple.CoreSimulator.SimRuntime.tvOS-27-0")
