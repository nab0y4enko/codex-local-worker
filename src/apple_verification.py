"""Small Apple simulator metadata helpers used by native verification."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict


UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


class ApplePolicyError(ValueError):
    """A simulator identifier or device listing is invalid."""


def validate_simulator_udid(value: str) -> str:
    """Accept only a bare UUID suitable for use as a simulator selector."""
    if not isinstance(value, str):
        raise ApplePolicyError(f"simulator UDID must be a string, got {type(value).__name__}")
    candidate = value.strip()
    if not UDID_RE.fullmatch(candidate):
        raise ApplePolicyError(f"simulator UDID {value!r} is not a bare UUID")
    return candidate


def simctl_devices(run: Callable[[list[str]], Any]) -> Dict[str, Dict]:
    """Return simctl devices indexed by UDID, retaining their runtime identifier."""
    result = run(["/usr/bin/xcrun", "simctl", "list", "devices", "available", "--json"])
    if isinstance(result, dict):
        stdout = result.get("stdout", "")
        returncode = result.get("returncode", 0)
    else:
        stdout = getattr(result, "stdout", "")
        returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        raise ApplePolicyError("simctl device listing failed")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    try:
        listing = json.loads(stdout)
    except (TypeError, ValueError) as error:
        raise ApplePolicyError("simctl device listing is not valid JSON") from error
    runtimes = listing.get("devices") if isinstance(listing, dict) else None
    if not isinstance(runtimes, dict):
        raise ApplePolicyError("simctl device listing has no devices object")
    devices = {}
    for runtime, entries in runtimes.items():
        if not isinstance(runtime, str) or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            udid = entry.get("udid")
            if isinstance(udid, str) and UDID_RE.fullmatch(udid):
                devices[udid] = dict(entry, runtime=runtime)
    return devices
