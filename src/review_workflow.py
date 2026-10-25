"""Deterministic job orchestration and an explicit cloud semantic review boundary.

No inference is performed here. Reports are evidence, never semantic approval.
"""
from __future__ import annotations

import argparse
import uuid

import acceptance_checks
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict

import contract
import ledger
import local_worker as lw
import permissions
import read_answers
import worktree_lifecycle as lifecycle


class WorkflowError(ValueError):
    pass


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PATCH_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 128 * 1024
REVIEW_SECTIONS = {'diff', 'sources', 'tests', 'findings', 'audit', 'screenshots'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def regular(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise WorkflowError('evidence is not a regular, non-symlink file: %s' % path)
    return path.resolve(strict=True)


def read_json(path):
    path = regular(path)
    with path.open('rb') as handle:
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise WorkflowError('JSON evidence exceeds size limit: %s' % path)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise WorkflowError('expected JSON object: %s' % path)
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise WorkflowError('refusing symlink output: %s' % path)
    fd, temporary = tempfile.mkstemp(prefix='.workflow-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def job_paths(state, job):
    if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}', job):
        raise WorkflowError('invalid job ID')
    state = Path(state).resolve()
    return state / 'reports' / job / 'report.json', state / 'reviews' / job / 'package.json'


def _bind(path, bindings):
    path = regular(path)
    bindings[str(path)] = lifecycle.hash_file(path)
    return path


def _checks_complete(report):
    if report.get('status') != 'ok' or report.get('violations') != []:
        return False
    if report.get('kind', 'write') == 'write':
        commands = report.get('contract', {}).get('test_commands', [])
        tests = report.get('tests', [])
        if not commands or not tests or any(t.get('status') != 'passed' for t in tests):
            return False
        if any(not any(t.get('command') == command and t.get('status') == 'passed'
                       for t in tests) for command in commands):
            return False
    if report.get('kind') == 'read':
        citations = report.get('citations')
        verified = report.get('citation_verification') or {}
        audit = report.get('read_audit') or {}
        if (not isinstance(citations, list) or not citations or
                verified.get('total') != len(citations) or verified.get('verified') != len(citations) or
                verified.get('failures') != [] or audit.get('changed_paths') != [] or
                audit.get('violations') != [] or audit.get('no_changes_required') is not True):
            return False
    if report.get('contract', {}).get('acceptance_checks'):
        if report.get('acceptance_checks', {}).get('status') != 'passed':
            return False
    return True


def build_package(state, job, *, max_patch_bytes=MAX_PATCH_BYTES):
    """Build review material from launcher evidence; never trust a worker summary."""
    state = Path(state).resolve()
    report_path, package_path = job_paths(state, job)
    with lifecycle.archive_lock(state):
        report = read_json(report_path)
        if report.get('job_id') != job:
            raise WorkflowError('report job identity mismatch')
        repo = Path(report['repo_path']).resolve()
        tree, _ = lifecycle.resolve_worktree(repo, state, report)
        bindings = {}
        _bind(report_path, bindings)
        incomplete = []
        worker_snapshot = lw.primary_fingerprint(tree)
        if report.get('worker_fingerprint_after') != worker_snapshot:
            incomplete.append('Candidate changed since verification or verified fingerprint unavailable.')
        task = report.get('contract')
        if not isinstance(task, dict):
            incomplete.append('Original task contract unavailable; rerun with a current launcher.')
        patch = {'path': report.get('patch_path'), 'sha256': None, 'text': '', 'truncated': False}
        if report.get('kind', 'write') == 'write':
            try:
                path = _bind(report['patch_path'], bindings)
                path.relative_to(report_path.parent)
                patch['sha256'] = bindings[str(path)]
                with path.open('rb') as handle:
                    data = handle.read(max_patch_bytes + 1)
                patch['truncated'] = len(data) > max_patch_bytes
                patch['text'] = data[:max_patch_bytes].decode('utf-8', 'replace')
                if patch['truncated'] or not data:
                    incomplete.append('Patch missing, empty or exceeds review package limit.')
            except (KeyError, TypeError, OSError, ValueError) as error:
                incomplete.append('Patch unavailable: %s' % error)
        sources = {}
        remaining = MAX_SOURCE_BYTES
        source_names = list(report.get('changed_paths', []))
        if report.get('kind') == 'read':
            citations = report.get('citations') or []
            envelope = {'answer': report.get('answer'), 'citations': citations}
            schema = read_answers.envelope_schema((task or {}).get('answer_schema', {}))
            if (read_answers.validate(envelope, schema) or
                    read_answers.verify_citations(tree, citations).get('failures')):
                incomplete.append('Read answer schema or current source citations do not verify.')
            source_names.extend(c['path'] for c in citations
                                if isinstance(c, dict) and isinstance(c.get('path'), str))
        for name in dict.fromkeys(source_names):
            candidate = tree / name
            if Path(name).is_absolute() or '..' in Path(name).parts:
                incomplete.append('Unsafe changed source path: %s' % name)
                continue
            if not candidate.exists() and not candidate.is_symlink():
                sources[name] = {'deleted': True}
                continue
            try:
                candidate = _bind(candidate, bindings)
                candidate.relative_to(tree.resolve())
                with candidate.open('rb') as handle:
                    data = handle.read(max(0, remaining) + 1)
                truncated = len(data) > remaining
                sources[name] = {'text': data[:max(0, remaining)].decode('utf-8', 'replace'),
                                 'truncated': truncated, 'sha256': bindings[str(candidate)]}
                if truncated:
                    incomplete.append('Source context truncated: %s; inspect full file.' % name)
                remaining = max(0, remaining - len(data))
            except (OSError, ValueError) as error:
                incomplete.append('Source unavailable: %s (%s)' % (name, error))
        evidence = list(report.get('logs', []))
        if task and task.get('acceptance_checks'):
            try:
                manifest = acceptance_checks.load_manifest(Path(task['acceptance_checks']), repo=repo,
                                                          writable_roots=[tree])
                if not acceptance_checks.matches_reported_pins(
                        manifest, report.get('acceptance_checks', {}).get('input_pins', [])):
                    incomplete.append('Trusted acceptance inputs changed since worker verification.')
                evidence.extend(str(p) for p in manifest.protected_paths)
            except (OSError, ValueError) as error:
                incomplete.append('Trusted acceptance evidence unavailable: %s' % error)
        if (state / 'permissions.json').exists():
            evidence.append(str(state / 'permissions.json'))
        # Capture every regular screenshot artifact without expanding binary images into JSON.
        screenshot_dir = report_path.parent / 'screenshots'
        if screenshot_dir.is_dir():
            evidence.extend(str(p) for p in screenshot_dir.rglob('*') if p.is_file())
        for item in evidence:
            try:
                _bind(item, bindings)
            except (OSError, ValueError) as error:
                incomplete.append('Evidence unavailable: %s' % error)
        if not _checks_complete(report):
            incomplete.append('Required job verification/audit did not pass or is unavailable.')
        package = dict(version=1, job_id=job, semantic_review_required=True,
            complete=not incomplete, incomplete_reasons=incomplete,
            report_path=str(report_path), repo_path=str(repo), worktree=str(tree),
            base_commit=report.get('base_commit'), kind=report.get('kind', 'write'),
            primary_snapshot=lw.primary_fingerprint(repo),
            worker_snapshot=worker_snapshot,
            contract=task, patch=patch, sources=sources,
            checks={key: report.get(key) for key in ('status', 'tests', 'acceptance_checks',
                'violations', 'review_findings', 'screenshots', 'native_preflight',
                'answer', 'answer_evidence', 'citations', 'citation_verification', 'read_audit',
                'classification', 'permissions')},
            bindings=bindings, package_path=str(package_path))
        package['package_sha256'] = digest(package)
        atomic_json(package_path, package)
        return package


def _validate_package(package):
    unsigned = dict(package)
    expected = unsigned.pop('package_sha256', None)
    if expected != digest(unsigned):
        raise WorkflowError('review package changed')
    for path, expected in package['bindings'].items():
        if lifecycle.hash_file(regular(path)) != expected:
            raise WorkflowError('reviewed evidence changed: %s' % path)


def _commands(value):
    if not isinstance(value, list) or any(not isinstance(c, list) or not c or
            any(not isinstance(a, str) or not a or '\x00' in a for a in c) for c in value):
        raise WorkflowError('combined_commands must be argv arrays')
    return value


def _integration_checks(task, report, state, repo, directory, commands):
    """Run with the existing bounded verifier and current permission policy.

    Primary source stays read-only under Seatbelt. Checks needing writable build output
    must target external scratch; otherwise they fail visibly instead of mutating reviewed code.
    """
    import copy
    verification = copy.deepcopy(task)
    verification.test_commands = commands
    deadline = lw.Deadline(task.timeout_seconds)
    perms = permissions.load_permissions(state)
    record = {'denials': [], 'deny_list_applied': False}
    wrapper = lw.DenyWrapper(perms, repo, state, record)
    if not permissions.wrapper_available():
        raise WorkflowError('integration requires an enforceable permission wrapper')
    manifest = None
    if getattr(task, 'acceptance_checks', None):
        manifest = acceptance_checks.load_manifest(task.acceptance_checks, repo=repo)
        if not acceptance_checks.matches_reported_pins(
                manifest, report.get('acceptance_checks', {}).get('input_pins', [])):
            raise WorkflowError('trusted acceptance inputs changed since worker verification')
    profile = directory / 'integration-permissions.sb'
    profile.write_text(permissions.seatbelt_profile(
        perms, primary_repo=repo, state_dir=state,
        deny_write_subpaths=[str(p) for p in manifest.protected_paths] if manifest else ()))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    probe = lw.run_bounded(permissions.wrap_argv(lw.WRAPPER_PROBE_ARGV, profile),
                           cwd=repo, env=env, timeout=deadline.cap(lw.PROBE_TIMEOUT_SECONDS))
    if probe['returncode'] != 0 or probe['timed_out'] or probe.get('output_limit_exceeded'):
        raise WorkflowError('integration permission wrapper probe failed')
    wrapper.profile = profile
    record['deny_list_applied'] = True
    results = lw.run_verification(verification, repo, env, lw.resolve_codex_bin(None),
                                 {'available': True}, [], deadline, wrapper)
    if manifest:
        results.extend(lw.run_acceptance_checks(task, manifest, report, repo, env,
                       lw.resolve_codex_bin(None), {'available': True}, [], deadline, wrapper))
    for index, item in enumerate(results):
        output = item.pop('output_text', '')
        path = directory / ('integration-%d.log' % index)
        path.write_text(output)
        item['log_path'] = str(path)
    return results


def decide(state, job, decision):
    """Explicit cloud reviewer decision; passing checks alone cannot enter this path."""
    state = Path(state).resolve()
    report_path, package_path = job_paths(state, job)
    outcome_path = package_path.parent / 'decision-result.json'
    action = decision.get('action')
    if action not in ('accept', 'reject'):
        raise WorkflowError('action must be accept or reject')
    if decision.get('semantic_review') is not True or decision.get('reviewer') != 'cloud-planner':
        raise WorkflowError('explicit cloud semantic review is required')
    if not isinstance(decision.get('rationale'), str) or not decision['rationale'].strip():
        raise WorkflowError('semantic review rationale is required')
    sections = decision.get('reviewed_sections')
    if (not isinstance(sections, list) or any(not isinstance(s, str) for s in sections) or
            not REVIEW_SECTIONS.issubset(set(sections))):
        raise WorkflowError('semantic reviewer must inspect all required review sections')
    with lifecycle.archive_lock(state):
        package = read_json(package_path)
        if decision.get('package_sha256') != package.get('package_sha256'):
            raise WorkflowError('decision does not match review package')
        _validate_package(package)
        if outcome_path.exists():
            raise WorkflowError('decision already recorded; inspect decision-result.json before proceeding')
        repo = Path(package['repo_path'])
        report = read_json(report_path)
        tree, _ = lifecycle.resolve_worktree(repo, state, report)
        if lw.primary_fingerprint(repo) != package['primary_snapshot']:
            raise WorkflowError('primary checkout changed since review')
        if lw.primary_fingerprint(tree) != package['worker_snapshot']:
            raise WorkflowError('worker checkout changed since review')
        result = dict(version=1, job_id=job, decision=decision, applied=False,
                      status='rejected' if action == 'reject' else 'accepting',
                      integration_tests=[], archive=None)
        if action == 'accept':
            if not package.get('complete'):
                raise WorkflowError('review package is incomplete')
            if not _checks_complete(report):
                raise WorkflowError('required checks did not pass')
            if lw.rev_parse(repo, 'HEAD') != package['base_commit']:
                raise WorkflowError('primary checkout base changed')
            task = contract.Contract.from_payload(package['contract'])
            if task.kind == 'write':
                dirty = [entry[1] for entry in package['primary_snapshot']['entries']]
                changed = report.get('changed_paths', [])
                if any(a == b or a.startswith(b.rstrip('/') + '/') or b.startswith(a.rstrip('/') + '/')
                       for a in dirty for b in changed):
                    raise WorkflowError('patch paths overlap existing dirty work')
                commands = list(task.test_commands) + _commands(decision.get('combined_commands', []))
                patch_path = package['patch']['path']
                patch_bytes = regular(patch_path).read_bytes()
                if hashlib.sha256(patch_bytes).hexdigest() != package['patch']['sha256']:
                    raise WorkflowError('reviewed patch changed before integration')
                # Exact reviewed bytes: do not normalize or silently edit at integration time.
                checked = lw.git(repo, 'apply', '--check', '--whitespace=error', stdin=patch_bytes, check=False)
                if checked.returncode:
                    raise WorkflowError('reviewed patch cannot be applied cleanly: ' + checked.stderr.decode('utf-8', 'replace'))
                atomic_json(outcome_path, result)  # crash leaves an explicit interrupted transaction
                lw.git(repo, 'apply', '--whitespace=error', stdin=patch_bytes)
                result['applied'] = True
                atomic_json(outcome_path, result)
                after_apply = lw.primary_fingerprint(repo)
                try:
                    result['integration_tests'] = _integration_checks(task, report, state, repo,
                                                                      package_path.parent, commands)
                    _validate_package(package)
                    if (any(t.get('status') != 'passed' for t in result['integration_tests']) or
                            lw.primary_fingerprint(repo) != after_apply):
                        result['status'] = 'integration_failed'
                        result['reason'] = 'Combined checks failed or changed reviewed source; changes preserved.'
                        atomic_json(outcome_path, result)
                        return result
                except Exception as error:
                    result.update(status='integration_failed', reason=str(error))
                    atomic_json(outcome_path, result)
                    return result
            result['status'] = 'accepted'
        atomic_json(outcome_path, result)
    # archive() obtains the same exclusive pool lock itself; never nest flock acquisition.
    try:
        receipt = lifecycle.archive(repo, state, job)
        problem = lifecycle.verify_evidence(repo, state, job, receipt)
        if problem:
            raise WorkflowError(problem)
        result['archive'] = receipt
    except (lifecycle.LifecycleError, OSError, ValueError) as error:
        result['status'] = 'accepted_archive_pending' if action == 'accept' else 'rejected_archive_pending'
        result['reason'] = str(error)
    try:
        session = ledger.session_id()
        md, js = ledger.write_summary(state, session, Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser())
        result['summary'] = {'markdown': str(md), 'json': str(js), 'session': session}
    except (OSError, ValueError) as error:
        result['summary_error'] = str(error)
    atomic_json(outcome_path, result)
    return result


def _run_launcher(argv, *, timeout, **_kwargs):
    # The shared process supervisor sends TERM before KILL and drains bounded output;
    # this gives the launcher its normal interruption cleanup, including worker groups.
    result = lw.run_bounded(argv, cwd=None, env=dict(os.environ), timeout=timeout)
    if result['timed_out']:
        raise subprocess.TimeoutExpired(argv, timeout, output=result['stdout'], stderr=result['stderr'])
    if result.get('output_limit_exceeded'):
        raise WorkflowError('launcher output exceeded capture limit; inspect retained report')
    return subprocess.CompletedProcess(argv, result['returncode'],
        result['stdout'].decode('utf-8', 'replace'), result['stderr'].decode('utf-8', 'replace'))


def orchestrate(manifest_path, state, *, run=None, launcher_args=None):
    """One invocation runs the declared queue sequentially; no polling or cloud inference."""
    run = run or _run_launcher
    manifest = read_json(manifest_path)
    tasks = manifest.get('tasks')
    if set(manifest) != {'tasks'} or not isinstance(tasks, list) or not tasks or len(tasks) > 32:
        raise WorkflowError('queue requires 1..32 task file paths')
    state = Path(state).resolve()
    queue_root = state / 'queues' / uuid.uuid4().hex
    pinned = []
    for index, path in enumerate(tasks):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise WorkflowError('task paths must be absolute')
        payload = read_json(path)
        normalized = contract.Contract.from_payload(payload).to_dict()
        snapshot = queue_root / ('task-%d.json' % index)
        atomic_json(snapshot, normalized)
        pinned.append({'original_path': path, 'original_sha256': lifecycle.hash_file(regular(path)),
                       'snapshot_path': str(snapshot), 'snapshot_sha256': lifecycle.hash_file(snapshot),
                       'contract': normalized})
    result = {'status': 'awaiting_cloud_review', 'jobs': [], 'pending_tasks': list(tasks),
              'input_pins': pinned, 'manifest_sha256': digest(manifest),
              'receipt_path': str(queue_root / 'receipt.json')}
    atomic_json(result['receipt_path'], result)
    for entry in pinned:
        path = entry['original_path']
        item = {'task_file': path, 'job_id': None}
        try:
            if (lifecycle.hash_file(regular(path)) != entry['original_sha256'] or
                    lifecycle.hash_file(regular(entry['snapshot_path'])) != entry['snapshot_sha256']):
                raise WorkflowError('queued task changed after validation')
            argv = [sys.executable, str(Path(__file__).with_name('local_worker.py')), 'run',
                    '--task-file', entry['snapshot_path'], '--state-dir', str(state),
                    '--max-workers', '1', '--json']
            argv.extend(launcher_args or [])
            completed = run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=entry['contract']['timeout_seconds'] + 30)
            try:
                output = json.loads(completed.stdout)
                if not isinstance(output, dict):
                    raise ValueError('not an object')
            except (TypeError, ValueError):
                output = {'status': 'invalid_launcher_output'}
            item.update(status=output.get('status'), job_id=output.get('job_id'))
            if output.get('job_id'):
                package = build_package(state, output['job_id'])
                if package.get('contract') != entry['contract']:
                    raise WorkflowError('executed contract differs from pinned queue contract')
                item.update(package_path=package['package_path'],
                            package_sha256=package['package_sha256'], complete=package['complete'])
            if completed.returncode or output.get('status') != 'ok' or not item.get('complete'):
                result['status'] = 'blocked'
        except subprocess.TimeoutExpired:
            item.update(status='launcher_timeout', reason='Launcher exceeded task budget plus shutdown grace; inspect retained state.')
            result['status'] = 'blocked'
        except (OSError, ValueError, lifecycle.LifecycleError) as error:
            item.update(status='blocked', reason=str(error))
            result['status'] = 'blocked'
        result['jobs'].append(item)
        result['pending_tasks'].pop(0)
        atomic_json(result['receipt_path'], result)
        if result['status'] == 'blocked':
            break
    atomic_json(result['receipt_path'], result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog='local-worker workflow')
    sub = parser.add_subparsers(dest='command', required=True)
    queue = sub.add_parser('orchestrate')
    queue.add_argument('--manifest', type=Path, required=True)
    queue.add_argument('--settings')
    queue.add_argument('--codex-bin')
    package = sub.add_parser('review-package')
    package.add_argument('--job-id', required=True)
    decision = sub.add_parser('decide')
    decision.add_argument('--job-id', required=True)
    decision.add_argument('--decision', type=Path, required=True)
    for command in (queue, package, decision):
        command.add_argument('--state-dir', type=Path)
    args = parser.parse_args(argv)
    state = lw.resolve_state_dir(str(args.state_dir) if args.state_dir else None)
    try:
        if args.command == 'orchestrate':
            extra = []
            for flag in ('settings', 'codex_bin'):
                if getattr(args, flag):
                    extra += ['--' + flag.replace('_', '-'), getattr(args, flag)]
            value = orchestrate(args.manifest, state, launcher_args=extra)
        elif args.command == 'review-package':
            value = build_package(state, args.job_id)
        else:
            value = decide(state, args.job_id, read_json(args.decision))
        displayed = value
        if args.command == 'orchestrate':
            displayed = {key: value[key] for key in ('status', 'jobs', 'pending_tasks', 'receipt_path')}
        elif args.command == 'decide':
            displayed = {key: value.get(key) for key in
                         ('status', 'job_id', 'applied', 'reason', 'summary', 'summary_error')}
            displayed['receipt_path'] = str(job_paths(state, args.job_id)[1].parent / 'decision-result.json')
            displayed['integration_checks'] = [
                {key: test.get(key) for key in ('command', 'name', 'status', 'reason', 'log_path')}
                for test in value.get('integration_tests', [])]
            displayed['archive_verified'] = bool(value.get('archive'))
        print(json.dumps(displayed, indent=2))
        return 0 if value.get('status') not in ('blocked', 'integration_failed', 'accepted_archive_pending', 'rejected_archive_pending') else 1
    except (WorkflowError, OSError, ValueError, lifecycle.LifecycleError, lw.LauncherError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
