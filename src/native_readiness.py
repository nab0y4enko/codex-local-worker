"""Fail-closed native qualification, using only the caller's confined runner.

This module never starts processes or changes sandbox permissions. ``run(argv)``
MUST use the same profile, environment, cwd and shared deadline as later native
verification. The caller owns that confinement; a successful gate proves only
this plan, artifact and selected simulator, not physical-device qualification.

Version 1 plans contain exactly: version, build, install, launch, test,
app_bundle, result_bundle, bundle_id. Commands are argv lists, with optional
{worktree}, {report_dir}, {simulator_udid}, {app_bundle}, {result_bundle}, and
{bundle_id} substitutions. Relative paths are worktree-relative. Artifacts may
live only in that worktree or report_dir/apple-scratch and must be fresh.
"""
from __future__ import annotations

import json
import plistlib
import re
import time
from pathlib import Path
from typing import Any, Callable


class NativeReadinessError(RuntimeError):
    def __init__(self, message: str, evidence: dict):
        super().__init__(message)
        self.evidence = evidence


_KEYS = {'version', 'build', 'install', 'launch', 'test',
         'app_bundle', 'result_bundle', 'bundle_id'}
_UDID = re.compile(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}')
_PATH_FLAGS = {'-project', '-workspace', '-derivedDataPath', '-clonedSourcePackagesDirPath',
               '-testProductsPath', '-xctestrun', '-resultBundlePath', '-resultStreamPath',
               '-xcconfig', '-testPlanPath'}
_PATH_SETTINGS = {'SYMROOT', 'OBJROOT', 'DSTROOT', 'CONFIGURATION_BUILD_DIR',
                  'CONFIGURATION_TEMP_DIR', 'BUILD_DIR', 'BUILD_ROOT', 'PROJECT_TEMP_DIR',
                  'SHARED_PRECOMPS_DIR', 'CLANG_MODULE_CACHE_PATH', 'SWIFT_MODULE_CACHE_PATH'}
_ACTIONS = {'build', 'build-for-testing', 'test', 'test-without-building', 'clean',
            'archive', 'analyze', 'install', 'installsrc', 'installhdrs', 'exportArchive'}


def _path(value: str, worktree: Path, roots: tuple[Path, ...], label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{label}: empty or unsafe path')
    original = Path(value)
    candidate = original if original.is_absolute() else worktree / original
    if '..' in candidate.parts:
        raise ValueError(f'{label}: path traversal is not allowed')
    # Check every existing component, including a dangling final symlink. Merely
    # resolving then checking containment would permit aliases and stale artifacts.
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise ValueError(f'{label}: symlink path is not allowed: {component}')
    resolved = candidate.resolve()
    if not any(resolved != root and root in resolved.parents for root in roots):
        raise ValueError(f'{label}: path must be inside the worktree or report scratch')
    relative = resolved.relative_to(next(root for root in roots if root in resolved.parents))
    if any(part in {'.git', '.codex', '.agents'} for part in relative.parts):
        raise ValueError(f'{label}: protected path is not allowed')
    return resolved


def _substitute(value: str, values: dict[str, str]) -> str:
    for key, replacement in values.items():
        value = value.replace('{' + key + '}', replacement)
    if '{' in value or '}' in value:
        raise ValueError('unknown native plan placeholder')
    return value


def _option(argv: list[str], flag: str, stage: str) -> str:
    if argv.count(flag) != 1:
        raise ValueError(f'{stage}: exactly one {flag} is required')
    index = argv.index(flag)
    if index + 1 >= len(argv) or not argv[index + 1].strip():
        raise ValueError(f'{stage}: {flag} needs a value')
    return argv[index + 1]


def _xcode(argv: list[str], stage: str, udid: str, worktree: Path,
           roots: tuple[Path, ...], result: Path) -> None:
    if argv[0] not in ('xcodebuild', '/usr/bin/xcodebuild'):
        raise ValueError(f'{stage}: direct xcodebuild command required')
    argv[0] = '/usr/bin/xcodebuild'
    allowed = {'build', 'build-for-testing'} if stage == 'build' else {'test', 'test-without-building'}
    # Parse option values separately: a scheme named "test" is not a test action.
    action_tokens = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in _PATH_FLAGS:
            value = _option(argv, token, stage)
            argv[index + 1] = str(_path(value, worktree, roots, stage + ' ' + token))
            index += 2
            continue
        if token.startswith('-') and index + 1 < len(argv) and not argv[index + 1].startswith('-'):
            # Actions do not follow valueless flags in qualified plans. Those
            # flags are handled explicitly so they cannot hide a test action.
            if token not in {'-quiet', '-verbose', '-disableAutomaticPackageResolution',
                             '-onlyUsePackageVersionsFromResolvedFile', '-skipPackageUpdates',
                             '-skipPackagePluginValidation', '-skipMacroValidation',
                             '-disable-concurrent-testing', '-enableCodeCoverage'}:
                index += 2
                continue
        if token in _ACTIONS:
            action_tokens.append(token)
        if '=' in token and not token.startswith('-'):
            key, value = token.split('=', 1)
            if key in _PATH_SETTINGS or value.startswith('/') or value.startswith('../'):
                _path(value, worktree, roots, stage + ' build setting ' + key)
        elif token.startswith('/') or token.startswith('../'):
            _path(token, worktree, roots, stage)
        index += 1
    if len(action_tokens) != 1 or action_tokens[0] not in allowed:
        raise ValueError(f'{stage}: exactly one native {stage} action is required')
    destination = _option(argv, '-destination', stage)
    fields = destination.split(',')
    ids = [field.split('=', 1)[1] for field in fields if field.startswith('id=')]
    if len(ids) != 1 or ids[0].upper() != udid.upper():
        raise ValueError(f'{stage}: destination must bind the selected simulator UDID')
    if any(field.startswith(('name=', 'platform=macOS')) for field in fields):
        raise ValueError(f'{stage}: conflicting destination selector')
    if stage == 'test' and _option(argv, '-resultBundlePath', stage) != str(result):
        raise ValueError('test: resultBundlePath must match the fresh result bundle')
    if stage == 'build' and '-resultBundlePath' in argv:
        raise ValueError('build: resultBundlePath is reserved for the test stage')


def _load(plan_path: Any, worktree: Path, report_dir: Path, udid: str) -> dict:
    if plan_path is None:
        raise ValueError('native readiness plan is required')
    plan = json.loads(Path(plan_path).read_text())
    if not isinstance(plan, dict) or set(plan) != _KEYS:
        raise ValueError('native readiness plan must contain exactly the version 1 fields')
    if type(plan['version']) is not int or plan['version'] != 1:
        raise ValueError('unsupported native readiness plan version')
    if not _UDID.fullmatch(udid):
        raise ValueError('selected simulator UDID must be a bare UUID')
    bundle = plan['bundle_id']
    if not isinstance(bundle, str) or not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', bundle):
        raise ValueError('bundle_id must be an explicit bundle identifier')
    roots = (worktree, report_dir / 'apple-scratch')
    values = {'worktree': str(worktree), 'report_dir': str(report_dir),
              'simulator_udid': udid, 'bundle_id': bundle}
    for key, suffix in (('app_bundle', '.app'), ('result_bundle', '.xcresult')):
        raw = plan[key]
        if not isinstance(raw, str):
            raise ValueError(f'{key}: path must be a string')
        path = _path(_substitute(raw, values), worktree, roots, key)
        if path.suffix != suffix:
            raise ValueError(f'{key}: expected a {suffix} path')
        if path.exists():
            raise ValueError(f'{key}: artifact already exists; a fresh path is required')
        values[key] = str(path)
        plan[key] = path
    if (plan['app_bundle'] in plan['result_bundle'].parents or
            plan['result_bundle'] in plan['app_bundle'].parents):
        raise ValueError('artifact paths must not overlap')
    for stage in ('build', 'install', 'launch', 'test'):
        argv = plan[stage]
        if not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or not arg.strip() or '\x00' in arg for arg in argv):
            raise ValueError(f'{stage}: nonempty argv strings required')
        plan[stage] = [_substitute(arg, values) for arg in argv]
    for stage in ('build', 'test'):
        _xcode(plan[stage], stage, udid, worktree, roots, plan['result_bundle'])
    for stage, target in (('install', str(plan['app_bundle'])), ('launch', bundle)):
        argv = plan[stage]
        if argv[0] not in ('xcrun', '/usr/bin/xcrun'):
            raise ValueError(f'{stage}: direct xcrun simctl command required')
        argv[0] = '/usr/bin/xcrun'
        if len(argv) != 5 or argv[1:3] != ['simctl', stage] or argv[3].upper() != udid.upper() or argv[4] != target:
            raise ValueError(f'{stage}: command must bind selected simulator and expected artifact or bundle identifier')
    return plan


def _counts(data: dict, label: str) -> tuple[int, int, int]:
    counts = [data.get(key) for key in ('passedTests', 'failedTests', 'skippedTests')]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError(f'{label}: missing or invalid XCTest counts')
    return tuple(counts)


def _summary(raw: str, udid: str) -> dict:
    summary = json.loads(raw)
    if not isinstance(summary, dict):
        raise ValueError('XCTest summary must be an object')
    passed, failed, skipped = _counts(summary, 'XCTest summary')
    if failed != 0 or summary.get('testFailures'):
        raise ValueError('XCTest summary contains failed tests')
    if passed <= 0:
        raise ValueError('XCTest summary has no passing tests; skipped tests do not qualify')
    if summary.get('result') != 'Passed':
        raise ValueError('XCTest summary result is not Passed')
    total = summary.get('totalTestCount')
    if type(total) is not int or total < passed + failed + skipped:
        raise ValueError('XCTest summary total count is inconsistent')
    devices = summary.get('devicesAndConfigurations')
    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list) or not devices:
        raise ValueError('XCTest summary has no device evidence')
    device_passed = 0
    for entry in devices:
        if not isinstance(entry, dict) or not isinstance(entry.get('device'), dict):
            raise ValueError('XCTest summary contains unknown device evidence')
        device = entry['device'].get('deviceId')
        if not isinstance(device, str) or device.upper() != udid.upper():
            raise ValueError('XCTest summary belongs to another device')
        p, f, _ = _counts(entry, 'XCTest device')
        if f:
            raise ValueError('XCTest device contains failed tests')
        device_passed += p
    if device_passed <= 0 or device_passed != passed:
        raise ValueError('XCTest device has no passing tests or inconsistent counts')
    return {'passed': passed, 'failed': failed, 'skipped': skipped,
            'total': total, 'device_id': udid, 'result': 'Passed'}


def qualify(plan_path: Any, worktree: Path, report_dir: Path, simulator_udid: str,
            run: Callable[[list[str]], Any], deadline: Any) -> dict:
    """Return persisted evidence or raise NativeReadinessError with .evidence.

    ``run`` returns a CompletedProcess-like object or dict containing returncode,
    stdout, stderr and optional timed_out. Callback exceptions fail closed. It
    must enforce the deadline during a command, not only between commands.
    """
    worktree, report_dir = Path(worktree).resolve(), Path(report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence = {'schema_version': 1, 'qualified': False, 'status': 'blocked',
                'simulator_udid': simulator_udid, 'checks': [],
                'scope': 'selected simulator only; no physical-device qualification'}
    active = 'plan'

    def persist():
        destination = report_dir / 'native-readiness.json'
        if destination.is_symlink():
            raise ValueError('native readiness evidence path is a symlink')
        destination.write_text(json.dumps(evidence, indent=2) + '\n')

    def budget():
        if deadline.remaining() <= 0:
            raise TimeoutError('native readiness shared budget exhausted')

    def execute(stage: str, argv: list[str]) -> str:
        nonlocal active
        active = stage
        budget()
        log = report_dir / f'native-readiness-{stage}.log'
        if log.is_symlink():
            raise ValueError(f'{stage}: log path is a symlink')
        check = {'name': stage, 'passed': False, 'argv': argv, 'log': str(log)}
        evidence['checks'].append(check)
        started = time.monotonic()
        try:
            result = run(argv)
            def field(name, default=None):
                return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)
            def text(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or '')
            stdout, stderr = text(field('stdout')), text(field('stderr'))
            log.write_text(stdout + '\n--- stderr ---\n' + stderr)
            check.update(returncode=field('returncode'), timed_out=bool(field('timed_out', False)))
            if check['timed_out']:
                raise TimeoutError(f'{stage}: timeout')
            budget()
            if type(check['returncode']) is not int or check['returncode'] != 0:
                raise ValueError(f'{stage}: native command failed ({check["returncode"]})')
            check['passed'] = True
            return stdout
        except Exception as exc:
            check['passed'] = False
            check['error'] = str(exc)
            if not log.exists():
                log.write_text(f'{type(exc).__name__}: {exc}\n')
            raise
        finally:
            check['elapsed_seconds'] = round(time.monotonic() - started, 6)

    try:
        budget()
        plan = _load(plan_path, worktree, report_dir, simulator_udid)
        evidence.update(bundle_id=plan['bundle_id'], app_bundle=str(plan['app_bundle']),
                        result_bundle=str(plan['result_bundle']))
        execute('build', plan['build'])
        app = _path(str(plan['app_bundle']), worktree,
                    (worktree, report_dir / 'apple-scratch'), 'built app bundle')
        if not app.is_dir():
            raise ValueError('build: expected fresh app bundle was not produced')
        info = app / 'Info.plist'
        if info.is_symlink() or not info.is_file():
            raise ValueError('build: app bundle Info.plist is missing or symlinked')
        if plistlib.loads(info.read_bytes()).get('CFBundleIdentifier') != plan['bundle_id']:
            raise ValueError('build: app bundle identifier does not match the plan')
        execute('install', plan['install'])
        output = execute('launch', plan['launch'])
        match = re.fullmatch(re.escape(plan['bundle_id']) + r':\s*([1-9][0-9]*)\s*', output.strip())
        if not match:
            raise ValueError('launch: expected bundle and positive PID evidence is missing')
        evidence['launch_pid'] = int(match.group(1))
        active = 'test'
        if plan['result_bundle'].exists() or plan['result_bundle'].is_symlink():
            raise ValueError('test: result artifact already exists before XCTest')
        execute('test', plan['test'])
        result = _path(str(plan['result_bundle']), worktree,
                       (worktree, report_dir / 'apple-scratch'), 'XCTest result bundle')
        if not result.is_dir():
            raise ValueError('test: fresh XCTest result bundle was not produced')
        output = execute('summary', ['/usr/bin/xcrun', 'xcresulttool', 'get', 'test-results',
                                     'summary', '--path', str(result), '--compact'])
        evidence['xctest'] = _summary(output, simulator_udid)
        budget()
        evidence.update(qualified=True, status='passed')
        persist()
        return evidence
    except Exception as exc:
        evidence.update(qualified=False, status='blocked', failed_stage=active,
                        failure_reason=f'{active}: {type(exc).__name__}: {exc}')
        # A zero command exit is not a passing stage if its artifact/evidence is invalid.
        if evidence['checks'] and evidence['checks'][-1]['name'] == active:
            evidence['checks'][-1].update(passed=False, error=str(exc))
        try:
            persist()
        except (OSError, ValueError) as storage_error:
            evidence['storage_error'] = str(storage_error)
        raise NativeReadinessError(evidence['failure_reason'], evidence) from exc
