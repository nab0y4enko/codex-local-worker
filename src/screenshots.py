"""Native screenshot capture, safe harness overlays and PNG blank-frame detection."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

import native_readiness as native


@dataclass(frozen=True)
class CaptureSource:
    name: str
    repo_path: Path
    ref: str
    scheme: Optional[str]
    destination: Dict[str, Any]


class _UnboundedDeadline:
    """Standalone capture-source use has no caller job budget."""
    def remaining(self) -> float:
        return 86400.0


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def png_is_uniform(path: Path) -> bool:
    """Decode 8-bit RGB/RGBA PNG scanlines and report whether every pixel is identical.

    Unsupported, truncated and corrupt files raise ``ValueError`` so callers can preserve the
    distinction between a blank image and an image that could not be inspected.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read PNG: {error}") from error
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG image")
    offset = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    saw_end = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        start, end = offset + 8, offset + 8 + length
        if end + 4 > len(data):
            raise ValueError("truncated PNG chunk")
        payload = data[start:end]
        expected_crc = struct.unpack(">I", data[end:end + 4])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != expected_crc:
            raise ValueError("PNG chunk checksum mismatch")
        if kind == b"IHDR":
            if width is not None or length != 13:
                raise ValueError("invalid PNG header")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload)
            if compression != 0 or filtering != 0:
                raise ValueError("unsupported PNG compression or filter method")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            saw_end = True
            if length != 0:
                raise ValueError("invalid PNG end chunk")
            offset = end + 4
            break
        offset = end + 4
    if not saw_end or offset != len(data) or width is None or height is None:
        raise ValueError("incomplete PNG image")
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError("unsupported PNG format; expected non-interlaced 8-bit RGB or RGBA")
    bytes_per_pixel = 3 if color_type == 2 else 4
    row_bytes = width * bytes_per_pixel
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as error:
        raise ValueError(f"corrupt PNG image data: {error}") from error
    if len(raw) != height * (row_bytes + 1):
        raise ValueError("PNG image data length does not match its dimensions")
    previous = bytearray(row_bytes)
    first_pixel = None
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        scanline = bytearray(raw[cursor:cursor + row_bytes])
        cursor += row_bytes
        if filter_type not in (0, 1, 2, 3, 4):
            raise ValueError(f"unsupported PNG filter {filter_type}")
        for index in range(row_bytes):
            left = scanline[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                predictor = 0
            scanline[index] = (scanline[index] + predictor) & 0xff
        for index in range(0, row_bytes, bytes_per_pixel):
            pixel = bytes(scanline[index:index + bytes_per_pixel])
            if first_pixel is None:
                first_pixel = pixel
            elif pixel != first_pixel:
                previous = scanline
                break
        else:
            previous = scanline
            continue
        return False
    return True


def _relative(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe repository-relative overlay path: {raw!r}")
    return path


def validate_source_name(value: Any) -> str:
    """Validate a source identifier before callers use it in an output path."""
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError("capture source name must be a safe identifier")
    return value


def _reject_symlink_ancestors(path: Path, label: str) -> Path:
    if ".." in Path(path).parts:
        raise ValueError(f"overlay {label} path contains traversal")
    absolute = Path(os.path.abspath(str(path)))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"overlay {label} path contains a symlink: {cursor}")
    return absolute


def apply_overlay(result_tree: Path, before_tree: Path, overlay_paths: List[str],
                  allowed_roots: List[str]) -> List[str]:
    """Copy only regular files under declared harness roots from result into before."""
    result_tree, before_tree = Path(result_tree), Path(before_tree)
    for root, label in ((result_tree, "source"), (before_tree, "destination")):
        root = _reject_symlink_ancestors(root, label)
        if not root.is_dir():
            raise ValueError(f"overlay {label} root is not a directory: {root}")
        if label == "source":
            result_tree = root
        else:
            before_tree = root
    result_tree, before_tree = result_tree.resolve(), before_tree.resolve()
    roots = [_relative(root) for root in allowed_roots]
    copied: List[str] = []
    for raw in overlay_paths:
        relative = _relative(raw)
        if not any(relative.parts[:len(root.parts)] == root.parts for root in roots):
            continue
        source = result_tree.joinpath(*relative.parts)
        cursor = result_tree
        unsafe = False
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                unsafe = True
                break
        if unsafe:
            raise ValueError(f"overlay source contains a symlink: {source}")
        if not source.is_file():
            continue
        target = before_tree.joinpath(*relative.parts)
        cursor = before_tree
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"overlay target contains a symlink: {cursor}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        copied.append(relative.as_posix())
    return sorted(copied)


def _success(result: Any, label: str) -> str:
    def field(name, default=None):
        return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)
    code = field("returncode")
    timed_out = bool(field("timed_out", False))
    stdout = field("stdout", "")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if timed_out:
        raise TimeoutError(f"{label} timed out")
    if type(code) is not int or code != 0:
        stderr = field("stderr", "")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} failed ({code}): {str(stderr).strip()[-300:]}")
    return str(stdout or "")


def _attachment(export_dir: Path, wanted: str) -> Path:
    manifest = next((path for path in export_dir.iterdir()
                     if path.is_file() and not path.is_symlink()
                     and path.name.lower() == "manifest.json"), None)
    candidates: List[str] = []
    if manifest is not None:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None

        def visit(value):
            if isinstance(value, dict):
                name_values = [str(value.get(key, "")) for key in
                               ("name", "attachmentName", "displayName",
                                "suggestedHumanReadableName")]
                matched = any(_xcode_attachment_name(name, wanted)
                              for name in name_values if name)
                if matched:
                    for key in ("exportedFileName", "fileName", "filename", "path"):
                        candidate = value.get(key)
                        if isinstance(candidate, str):
                            candidates.append(candidate)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(data)
    valid: set[Path] = set()
    for candidate in candidates:
        path = (export_dir / candidate).resolve()
        if export_dir.resolve() in path.parents and path.is_file() and not path.is_symlink():
            valid.add(path)
    if len(valid) == 1:
        return next(iter(valid))
    if len(valid) > 1:
        raise ValueError(f"multiple exported XCTest attachments match {wanted!r}")
    for path in export_dir.rglob("*"):
        if path.is_file() and not path.is_symlink() and _xcode_attachment_name(path.name, wanted):
            valid.add(path.resolve())
    if len(valid) == 1:
        return next(iter(valid))
    if len(valid) > 1:
        raise ValueError(f"multiple exported XCTest attachments match {wanted!r}")
    raise FileNotFoundError(f"named XCTest attachment {wanted!r} was not exported")


def _xcode_attachment_name(value: str, wanted: str) -> bool:
    """Match exact names or Xcode's ``name_index_UUID.png`` export suffix only."""
    if value == wanted:
        return True
    stem = Path(value).stem
    if stem == wanted:
        return True
    match = re.fullmatch(re.escape(wanted) + r"_([0-9]+)_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
                         r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})", stem)
    return match is not None


def _safe_screenshot_name(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError("screenshot name must be a safe file name")
    return value


def _wait(seconds: Any, deadline: Any) -> None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("settle_seconds must be a finite non-negative number")
    if deadline is None:
        time.sleep(seconds)
        return
    remaining = deadline.remaining()
    if remaining <= 0 or seconds > remaining:
        if remaining > 0:
            time.sleep(remaining)
        raise TimeoutError("job deadline exhausted during screenshot settle")
    if seconds:
        time.sleep(seconds)
    if deadline.remaining() <= 0:
        raise TimeoutError("job deadline exhausted during screenshot settle")


def capture_source(plan: Dict, source: CaptureSource, worktree: Path, out_dir: Path,
                   run: Callable, deadline: Any = None) -> List[Dict]:
    """Build and capture one source into its own artifact tree and screenshot directory."""
    validate_source_name(source.name)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshots = plan.get("screenshots") or []
    if not isinstance(screenshots, list) or not screenshots:
        return [{"name": "screenshots", "path": None, "method": "simctl",
                 "uniform": False, "error": "native plan has no requested screenshots"}]
    evidence_dir = out_dir.parent / "apple-scratch" / "capture-native" / source.name
    if evidence_dir.exists():
        raise ValueError(f"capture evidence directory already exists: {evidence_dir}")
    evidence_dir.mkdir(parents=True)
    product_dir = evidence_dir / "apple-scratch" / "products"
    derived_data = evidence_dir / "apple-scratch" / "derived-data"
    original_app = str(plan.get("app_bundle", "App.app")).replace("{worktree}", "")
    app_name = Path(original_app).name
    if not app_name.endswith(".app"):
        raise ValueError("native plan app_bundle must end in .app")
    app_bundle = product_dir / app_name
    result_bundle = evidence_dir / "apple-scratch" / "Tests.xcresult"
    source_plan = dict(plan)
    source_plan["version"] = 2
    source_plan["destination"] = dict(source.destination)
    source_plan["app_bundle"] = str(app_bundle)
    source_plan["result_bundle"] = str(result_bundle)
    if "simulator" in source.destination:
        install_argv = list(source_plan["install"])
        launch_argv = list(source_plan["launch"])
        if (len(install_argv) != 5 or install_argv[1:3] != ["simctl", "install"] or
                len(launch_argv) != 5 or launch_argv[1:3] != ["simctl", "launch"]):
            raise ValueError("simulator capture plan must use direct simctl install and launch")
        install_argv[3:] = ["{simulator_udid}", "{app_bundle}"]
        launch_argv[3:] = ["{simulator_udid}", "{bundle_id}"]
        source_plan["install"] = install_argv
        source_plan["launch"] = launch_argv
    for stage in ("build", "test"):
        argv = list(source_plan[stage])
        if source.scheme is not None and "-scheme" in argv:
            index = argv.index("-scheme")
            if index + 1 >= len(argv):
                raise ValueError(f"{stage}: -scheme needs a value")
            argv[index + 1] = source.scheme
        if "-destination" in argv:
            index = argv.index("-destination")
            selector = ("id=" + source.destination["simulator"]
                        if "simulator" in source.destination else "platform=macOS")
            argv[index + 1] = selector
        if stage == "test":
            if "-derivedDataPath" in argv:
                index = argv.index("-derivedDataPath")
                if index + 1 >= len(argv):
                    raise ValueError("test: -derivedDataPath needs a value")
                argv[index + 1] = str(derived_data)
            else:
                action_index = argv.index("test")
                argv[action_index:action_index] = ["-derivedDataPath", str(derived_data)]
            if "-resultBundlePath" in argv:
                index = argv.index("-resultBundlePath")
                if index + 1 >= len(argv):
                    raise ValueError("test: -resultBundlePath needs a value")
                argv[index + 1] = str(result_bundle)
            else:
                action_index = argv.index("test")
                argv[action_index:action_index] = ["-resultBundlePath", str(result_bundle)]
        source_plan[stage] = argv
    build_argv = source_plan["build"]
    configured_derived = False
    configured_products = False
    index = 0
    while index < len(build_argv):
        if build_argv[index] == "-derivedDataPath":
            if index + 1 >= len(build_argv):
                raise ValueError("build: -derivedDataPath needs a value")
            build_argv[index + 1] = str(derived_data)
            configured_derived = True
            index += 2
            continue
        if build_argv[index].startswith("CONFIGURATION_BUILD_DIR="):
            build_argv[index] = "CONFIGURATION_BUILD_DIR=" + str(product_dir)
            configured_products = True
        index += 1
    if not configured_derived:
        build_argv.extend(["-derivedDataPath", str(derived_data)])
    if not configured_products:
        build_argv.append("CONFIGURATION_BUILD_DIR=" + str(product_dir))
    source_plan["build"] = build_argv
    plan_file = evidence_dir / "native-plan.json"
    plan_file.write_text(json.dumps(source_plan, indent=2) + "\n", encoding="utf-8")
    evidence_dir.mkdir(exist_ok=True)
    native_deadline = deadline if deadline is not None else _UnboundedDeadline()

    def invoke(argv):
        require = getattr(native_deadline, "require", None)
        if require is not None:
            require("screenshot native command")
        return run(argv)

    try:
        evidence = native.qualify(plan_file, Path(worktree), evidence_dir,
                                  source.destination, invoke, native_deadline)
    except Exception as error:
        return [{"name": str(item.get("name") or "screenshot"), "path": None,
                 "method": "xcresult" if item.get("xcuitest_attachment") else "simctl",
                 "uniform": False, "error": f"source build/launch/test failed: {error}"}
                for item in screenshots if isinstance(item, dict)]

    results = []
    for item in screenshots:
        if not isinstance(item, dict):
            results.append({"name": "screenshot", "path": None, "method": "simctl",
                            "uniform": False, "error": "screenshot entry must be an object"})
            continue
        try:
            name = _safe_screenshot_name(item.get("name"))
            attachment = item.get("xcuitest_attachment")
            method = "xcresult" if attachment is not None else "simctl"
            path = out_dir / source.name / (name + ".png")
            path.parent.mkdir(parents=True, exist_ok=True)
            if attachment is not None:
                if not isinstance(attachment, str) or not attachment.strip():
                    raise ValueError("xcuitest_attachment must be a non-empty name")
                export_dir = evidence_dir / "attachments" / name
                export_dir.mkdir(parents=True, exist_ok=True)
                result = invoke(["/usr/bin/xcrun", "xcresulttool", "export", "attachments",
                                 "--path", str(result_bundle), "--output-path", str(export_dir)])
                _success(result, "xcresulttool attachment export")
                exported = _attachment(export_dir, attachment)
                if exported.suffix.lower() != ".png":
                    raise ValueError("named XCTest attachment is not a PNG")
                shutil.copyfile(str(exported), str(path))
            else:
                if "simulator" not in source.destination:
                    raise ValueError("simctl screenshots require a simulator destination")
                udid = source.destination["simulator"]
                url = item.get("url")
                if url is not None:
                    if not isinstance(url, str) or not url.strip():
                        raise ValueError("url must be a non-empty string")
                    launch_argv = ["/usr/bin/xcrun", "simctl", "openurl", udid, url]
                else:
                    launch_args = item.get("launch_arguments", [])
                    if not isinstance(launch_args, list) or any(not isinstance(arg, str) for arg in launch_args):
                        raise ValueError("launch_arguments must be an array of strings")
                    launch_argv = ["/usr/bin/xcrun", "simctl", "launch",
                                   "--terminate-running-process", udid, plan["bundle_id"], *launch_args]
                _success(invoke(launch_argv), "simulator screen launch")
                _wait(item.get("settle_seconds", 3), deadline)
                _success(invoke(["/usr/bin/xcrun", "simctl", "io", udid,
                                 "screenshot", str(path)]), "simulator screenshot")
            uniform = png_is_uniform(path)
            results.append({"name": name, "path": str(path), "method": method,
                            "uniform": uniform, "error": None})
        except Exception as error:
            results.append({"name": str(item.get("name") or "screenshot"), "path": None,
                            "method": "xcresult" if item.get("xcuitest_attachment") else "simctl",
                            "uniform": False, "error": str(error)})
    return results


def write_index(out_dir: Path, results: Dict[str, List[Dict]]) -> Path:
    """Write a source-comparison Markdown table with one row per screenshot name."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = list(results)
    by_source = {source: {entry.get("name"): entry for entry in entries}
                 for source, entries in results.items()}
    names = sorted({name for entries in by_source.values() for name in entries if name})
    lines = ["# Screenshot comparison", "", "| screenshot | " + " | ".join(sources) + " |",
             "| --- | " + " | ".join("---" for _ in sources) + " |"]
    for name in names:
        cells = []
        for source in sources:
            entry = by_source[source].get(name)
            if not entry:
                cell = "no image: not captured"
            elif entry.get("error"):
                cell = "no image: " + str(entry["error"]).replace("|", "\\|")
            elif entry.get("path"):
                path = Path(entry["path"]).resolve()
                try:
                    relative = path.relative_to(out_dir).as_posix()
                    cell = f"[image]({relative})"
                except ValueError:
                    cell = "no image: path outside screenshot directory"
            else:
                cell = "no image: unavailable"
            cells.append(cell)
        lines.append("| " + name.replace("|", "\\|") + " | " + " | ".join(cells) + " |")
    index = out_dir / "index.md"
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index
