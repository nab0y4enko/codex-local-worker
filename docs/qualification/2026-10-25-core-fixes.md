# Core readiness fix wave — 2026-10-25

## Scope and evidence boundary

This record covers the consolidated fixes for findings A1, A2, B1, B2, C1, C2, and the
permission-history minor. It is separate from the live qualification record: the offline suite
below passed against this tree, but no post-fix real local inference or native-device result is
claimed here yet.

## Git object isolation and audit

Write jobs receive a fresh `git-objects` directory under their report directory and the protected
primary object database only as a read alternate. Git environment variables are scrubbed and
pinned for launcher Git operations; read-only jobs omit the private write redirect, and capture
source clones do not inherit it. The worker may commit in its worktree without writing primary
objects. Before launcher audit Git commands can resolve objects through the primary alternate,
the launcher validates each private object in isolation, hashes its payload, and checks any OID
collision against the primary. Archive promotion revalidates the store and imports only validated,
equal payloads; an existing primary object is never overwritten. Legacy archive receipts without
private-store metadata remain supported.

Base-tree gitlinks are deliberately not traversed. Their state is reported as unknown and as an
audit violation, including when the gitlink appears unchanged. This avoids executing nested
repository configuration, filters, or hooks merely to inspect an untrusted checkout. A nested
repository change is therefore surfaced for review rather than treated as unchanged.

## Process ownership and deadlines

Launcher-owned long process groups are recorded before worker startup and kept under a stable
supervisor leader. Reaping requires matching launcher and owner birth identities, process group,
and session evidence. Process-table errors and mismatches preserve the record and surface
`ownership_uncertain_preserved`; they do not signal an unproven group. Escalation revalidates the
keeper immediately before `SIGKILL`. Probe and worker commands share the job's absolute deadline,
including Codex compatibility/version/help probes and oMLX discovery/rechecks; follow-up probes
are skipped when the budget is exhausted.

## Classification and metrics

First-try success requires one actual attempt, successful final status, zero exit, and no terminal
failure or timeout/stall/interruption. Sandbox-denied classification uses launcher-parsed path
protection evidence from completed command output, not worker prose or raw stderr. It distinguishes
known protected secret reads from protected out-of-scope writes and gives a concrete next action
without relaxing permissions. Permission history from global configuration is labeled as global,
not attributed to the current session.

## Offline verification

Command:

```sh
python3 -m unittest discover -s tests -v
```

Result: **1007 tests passed; 8 live-only tests skipped; 168.118 seconds**. The skipped tests are
opt-in live qualification cases and are not counted as evidence for this fix wave. Syntax check
passed with:

```sh
python3 -m py_compile src/local_worker.py src/process_supervisor.py src/worktree_lifecycle.py src/permissions.py src/classify.py src/ledger.py src/omlx_discovery.py src/worker_observability.py src/worker_tool_probe.py
```

Diff whitespace check passed with `git diff --check`.

## Live qualification

Post-fix real local write/read and native qualification is pending. The new qualification state,
job manifest, simulator identity, and exact commands/results will be recorded here and in the
external manifest after those runs. Cloud inference is not authorized and is not part of this
qualification.
