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
REVIEW_VIEW_NAME = 'review.md'


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


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise WorkflowError('refusing symlink output: %s' % path)
    fd, temporary = tempfile.mkstemp(prefix='.review-view-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fence(text):
    """A fence longer than any backtick run inside the quoted content."""
    longest = run = 0
    for char in text:
        if char == '`':
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return '`' * max(3, longest + 1)


def _block(text, language=''):
    fence = _fence(text)
    return '%s%s\n%s\n%s' % (fence, language, text, fence)


def _json_block(value):
    return _block(json.dumps(value, indent=2, sort_keys=True, default=str), 'json')


def _binding_summary(bindings):
    """Readable digest-bound bookkeeping: one line per retained file, no full hash map."""
    lines = ['%s  %s…  (full SHA-256 in the canonical package)' % (path, expected[:12])
             for path, expected in sorted(bindings.items())]
    return _block('\n'.join(lines), 'text')


def render_review(package):
    """Deterministic readable view of the canonical package, rendered from it alone.

    The canonical package, bindings and logs stay the evidence of record. The view exists so the
    calling cloud session can read everything once; it is verified byte-for-byte against a fresh
    rendering of the canonical package before any decision, so an edited or truncated view can
    never mislead review. Source and diff bytes are reproduced exactly: no JSON re-escaping.
    """
    task = package.get('contract') if isinstance(package.get('contract'), dict) else {}
    checks = package.get('checks') or {}
    patch = package.get('patch') or {}
    kind = package.get('kind', 'write')
    out = ['# Cloud review: %s' % package.get('job_id'), '',
           'The reviewer is the calling cloud planner session. Nothing in this view is semantic',
           'approval: `decide` still requires an explicit cloud decision bound to the exact',
           'package digest, all review sections, combined checks and a verified archive.', '',
           '- Canonical package: `%s`' % package.get('package_path'),
           '- Package SHA-256: `%s`' % package.get('package_sha256'),
           '- Job kind: `%s`' % kind,
           '- Base commit: `%s`' % package.get('base_commit'),
           '- Complete for acceptance: `%s`' % ('yes' if package.get('complete') else 'no'), '',
           '## Incomplete evidence', '']
    reasons = package.get('incomplete_reasons') or []
    out += (['- %s' % reason for reason in reasons] if reasons else ['- None recorded.']) + ['']

    out += ['## Frozen task contract', '']
    if task:
        out += [_json_block(task), 'Acceptance criteria:', '']
        criteria = task.get('acceptance_criteria') or []
        out += (['- %s' % item for item in criteria] if criteria
                else ['- None declared in this contract.']) + ['']
    else:
        out += ['- Original task contract unavailable; rerun with a current launcher.', '']

    out += ['## Full diff', '']
    if patch.get('truncated'):
        out += ['> TRUNCATED: the reviewed patch exceeds the review package byte limit. Read the '
                'full patch file before any decision; this view is not complete.', '']
    if patch.get('text'):
        out += [_block(patch['text'], 'diff'), '']
    else:
        out += ['- No patch recorded (%s job).' % kind, '']

    out += ['## Changed sources and tests', '']
    sources = package.get('sources') or {}
    if not sources:
        out += ['- No changed or cited source files recorded.', '']
    for name in sorted(sources):
        entry = sources[name] or {}
        out += ['### `%s`' % name, '']
        if entry.get('deleted'):
            out += ['- File is deleted in the candidate.', '']
            continue
        out += ['- SHA-256: `%s`' % entry.get('sha256'), '']
        if entry.get('truncated'):
            out += ['> TRUNCATED: only the first %d bytes of this file are shown; read the whole '
                    'file before any decision.' % MAX_SOURCE_BYTES, '']
        out += [_block(entry.get('text') or '', 'text'), '']

    out += ['## Actual checks', '',
            '- Launcher status: `%s`' % checks.get('status'),
            '- Violations: `%s`' % json.dumps(checks.get('violations'), sort_keys=True), '']
    tests = checks.get('tests')
    out += [_json_block(tests)] if tests else ['- No test records.']
    out += ['']

    def evidence(title, value, absent):
        out.append('## %s' % title)
        out.append('')
        out.append(_json_block(value) if value not in (None, [], {}, '')
                   else '- %s' % absent)
        out.append('')

    if kind == 'read':
        evidence('Read answer and citations',
                 {'answer': checks.get('answer'), 'answer_evidence': checks.get('answer_evidence'),
                  'citations': checks.get('citations'),
                  'citation_verification': checks.get('citation_verification')},
                 'No read answer or citations recorded.')
    evidence('Independent acceptance checks', checks.get('acceptance_checks'),
             'No independent acceptance checks are declared for this contract.')
    evidence('Review findings', checks.get('review_findings'), 'No review findings recorded.')
    recorded_audit = {key: checks[key] for key in ('read_audit', 'citation_verification')
                      if checks.get(key) not in (None, [], {}, '')}
    evidence('Audit', recorded_audit, 'No read/citation audit recorded.')
    if kind == 'write':
        evidence('Write scope and checkout snapshots',
                 {'violations': checks.get('violations'),
                  'changed_paths': sorted(package.get('sources') or {}),
                  'primary_snapshot': package.get('primary_snapshot'),
                  'worker_snapshot': package.get('worker_snapshot')},
                 'No write scope or checkout snapshot recorded.')
    evidence('Classification', checks.get('classification'), 'No classification recorded.')
    evidence('Permissions', checks.get('permissions'), 'No permission policy result recorded.')
    evidence('Native preflight', checks.get('native_preflight'), 'No native preflight recorded.')
    evidence('Screenshots', checks.get('screenshots'), 'No screenshots recorded.')
    bindings = package.get('bindings') or {}
    evidence('Screenshot artifacts bound as evidence',
             sorted(key for key in bindings if 'screenshots' in key),
             'No screenshot artifacts bound.')

    out += ['## Evidence bindings', '',
            '%d retained files are pinned by full SHA-256 in the canonical package; every one is '
            'rehashed before a decision is applied. The canonical package keeps the complete map; '
            'this view summarizes it so the review stays a single readable pass.' % len(bindings),
            '', _binding_summary(bindings), '']
    return '\n'.join(out)


def write_review_view(package):
    atomic_bytes(package['review_path'], render_review(package).encode('utf-8'))


def _overlaps(left, right):
    return (left == right or left.startswith(right.rstrip('/') + '/')
            or right.startswith(left.rstrip('/') + '/'))


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
            bindings=bindings, package_path=str(package_path),
            review_path=str(package_path.parent / REVIEW_VIEW_NAME))
        package['package_sha256'] = digest(package)
        atomic_json(package_path, package)
        # The readable view carries the package digest, so it is rendered after hashing and is
        # verified by exact bytes against the canonical package instead of being hashed into it.
        write_review_view(package)
        return package


def _validate_package(package):
    unsigned = dict(package)
    expected = unsigned.pop('package_sha256', None)
    if expected != digest(unsigned):
        raise WorkflowError('review package changed')
    for path, expected in package['bindings'].items():
        if lifecycle.hash_file(regular(path)) != expected:
            raise WorkflowError('reviewed evidence changed: %s' % path)
    view = package.get('review_path')
    if view:
        # The view is not hashed into the package (it embeds the package digest), so it is
        # verified by exact bytes against a fresh rendering. Read at most one byte past the
        # expected length: a tampered oversized view must never be slurped.
        expected = render_review(package).encode('utf-8')
        try:
            path = regular(view)
        except (OSError, ValueError):
            raise WorkflowError('review view is missing or not a regular file: %s' % view)
        with path.open('rb') as handle:
            actual = handle.read(len(expected) + 1)
        if actual != expected:
            raise WorkflowError('review view changed, was truncated, or exceeds the rendered '
                                'length: %s' % view)


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
                if any(_overlaps(a, b) for a in dirty for b in changed):
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


def _local_tier_guard(normalized, path):
    """Normal orchestration is local-only; a cloud tier needs separately authorized escalation."""
    tier = normalized.get('tier')
    if not isinstance(tier, dict) or 'cloud' in tier or tier.get('local') is not True:
        raise WorkflowError('%s requests a non-local tier; orchestrate runs local tier contracts '
                            'only and cloud escalation needs separate authorization' % path)


def _dirty_target_guard(normalized, path):
    """Never queue work that would write through the user's own dirty primary entries."""
    if normalized.get('kind', 'write') != 'write':
        return
    repo = Path(normalized['repo_path']).resolve()
    if not repo.is_dir():
        return
    try:
        dirty = lw.dirty_primary_files(repo)
    except (OSError, ValueError, lw.LauncherError) as error:
        raise WorkflowError('%s cannot audit dirty primary work in %s: %s; the overlap guard '
                            'cannot run, so nothing is queued before inference'
                            % (path, repo, error))
    for target in normalized.get('allowed_paths', []):
        for entry in dirty:
            if _overlaps(entry, target):
                raise WorkflowError('%s target %s overlaps dirty primary work %s; commit, stash, '
                                    'or narrow the contract before running inference'
                                    % (path, target, entry))


def _handoff(jobs):
    views = sorted({job['review_path'] for job in jobs if job.get('review_path')})
    return ('Handoff: the same cloud planner session that planned this assignment reviews %s in '
            'full, then posts one explicit `decide` bound to the exact package digest. No noop '
            'preparation session, no mandatory second reviewer, and no model polling; a process '
            'exit code is never semantic acceptance.'
            % (', '.join('`%s`' % view for view in views) if views else 'the canonical package'))


def orchestrate(manifest_path, state, *, run=None, launcher_args=None, task_file=None):
    """One invocation runs one assignment (or a declared queue) sequentially; no cloud inference."""
    run = run or _run_launcher
    if (manifest_path is None) == (task_file is None):
        raise WorkflowError('--task-file and --manifest are mutually exclusive: pass exactly one '
                            'entry point')
    if task_file is not None:
        tasks = [str(Path(task_file))]
        manifest_sha256 = digest({'tasks': tasks})
    else:
        manifest = read_json(manifest_path)
        tasks = manifest.get('tasks')
        if set(manifest) != {'tasks'} or not isinstance(tasks, list) or not tasks or len(tasks) > 32:
            raise WorkflowError('queue requires 1..32 task file paths')
        manifest_sha256 = digest(manifest)
    state = Path(state).resolve()
    queue_root = state / 'queues' / uuid.uuid4().hex
    pinned, identities = [], set()
    for index, path in enumerate(tasks):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise WorkflowError('task paths must be absolute')
        payload = read_json(path)
        normalized = contract.Contract.from_payload(payload).to_dict()
        _local_tier_guard(normalized, path)
        identity = (str(Path(normalized['repo_path']).resolve()), normalized['task_id'])
        if identity in identities:
            raise WorkflowError('duplicate task identity repo_path+task_id %s in one queue'
                                % (identity,))
        identities.add(identity)
        _dirty_target_guard(normalized, path)
        snapshot = queue_root / ('task-%d.json' % index)
        atomic_json(snapshot, normalized)
        pinned.append({'original_path': path, 'original_sha256': lifecycle.hash_file(regular(path)),
                       'snapshot_path': str(snapshot), 'snapshot_sha256': lifecycle.hash_file(snapshot),
                       'contract': normalized})
    result = {'status': 'awaiting_cloud_review', 'jobs': [], 'pending_tasks': list(tasks),
              'input_pins': pinned, 'manifest_sha256': manifest_sha256,
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
                            package_sha256=package['package_sha256'],
                            review_path=package.get('review_path'),
                            complete=package['complete'])
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
    result['handoff'] = _handoff(result['jobs'])
    atomic_json(result['receipt_path'], result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog='local-worker workflow')
    sub = parser.add_subparsers(dest='command', required=True)
    queue = sub.add_parser('orchestrate')
    entry = queue.add_mutually_exclusive_group(required=True)
    entry.add_argument('--task-file', type=Path,
                       help='default entry: one coherent bounded local assignment (absolute path)')
    entry.add_argument('--manifest', type=Path, help='legacy queue manifest of 1..32 task files')
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
            value = orchestrate(args.manifest, state, launcher_args=extra,
                                task_file=args.task_file)
        elif args.command == 'review-package':
            value = build_package(state, args.job_id)
        else:
            value = decide(state, args.job_id, read_json(args.decision))
        displayed = value
        if args.command == 'orchestrate':
            displayed = {key: value[key] for key in
                         ('status', 'jobs', 'pending_tasks', 'receipt_path', 'handoff')}
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
