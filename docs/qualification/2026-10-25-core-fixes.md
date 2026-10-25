# Core readiness fix wave — 2026-10-25

## Scope and evidence boundary

This record covers the consolidated fixes for findings A1, A2, B1, B2, C1, C2, and the
permission-history minor. It is separate from the live qualification reports, which are retained in the external manifest
listed below. The final offline suite and final post-fix real local/native qualification both
passed against commit `7465273`.

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
`ownership_uncertain_preserved`; they do not signal an unproven group. The supervisor installs
TERM/USR1 handlers before starting its command and holds the group leader after TERM until the
parent releases it or safely escalates; release uses bounded polling so a signal cannot be lost
between a check and a wait. Foreground cleanup revalidates the unreaped keeper before escalation
and never signals a numeric PGID after reaping it. Probe and worker commands share the job's
absolute deadline, including Codex compatibility/version/help, the cloud model catalog probe, and
oMLX discovery/rechecks; follow-up probes are skipped when the budget is exhausted. Real cloud
inference was not used.

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

Final result on commit `7465273`: **1013 tests total, 1005 executed successfully, 8 live-only
skips, 214.097 seconds**. Earlier staged checks were 1007 total/999 executed/8 skipped on
`a56c710` (168.118s) and 1011 total/1003 executed/8 skipped before the last signal-handler
ordering change (201.319s). The skipped tests are opt-in live qualification cases and are not
counted as executed passes. Syntax check
passed with:

```sh
python3 -m py_compile src/local_worker.py src/process_supervisor.py src/worktree_lifecycle.py src/permissions.py src/classify.py src/ledger.py src/omlx_discovery.py src/worker_observability.py src/worker_tool_probe.py
```

Diff whitespace check passed with `git diff --check`.

## Live qualification

The initial post-fix run on `a56c710` passed **8/8 cases in 196.908 seconds**. The final follow-up
run on `7465273` passed **8/8 cases in 165.160 seconds**. Both used the real
`bin/local-worker run` path for local write/read jobs, covered expected capability-failure and
context-overflow classifications, and qualified native build/install/launch/XCTest plus
before/after/reference screenshots on the newly created iPhone 17 simulator. The final strict
acceptance test `testAppShowsAfter` passed. Cloud inference was not called; the suite's cloud case
only checked catalog/argv behavior. The final run used this command and environment:

```sh
LOCAL_WORKER_LIVE=1 \
LOCAL_WORKER_QUALIFICATION_ROOT=/Users/oleksii.naboichenko/.local/share/codex-local-worker/core-fixwave-2026-10-25/final/qualification \
LOCAL_WORKER_QUALIFICATION_RECORD=/Users/oleksii.naboichenko/.local/share/codex-local-worker/core-fixwave-2026-10-25/final/qualification/live-report.md \
LOCAL_WORKER_LIVE_NATIVE_PLAN=/Users/oleksii.naboichenko/.local/share/codex-local-worker/core-fixwave-2026-10-25/final/native-pilot/native-plan.json \
LOCAL_WORKER_LIVE_NATIVE_SIMULATOR_UDID=4D3B2328-F90D-4703-9D83-956C7AFE5083 \
LOCAL_WORKER_LIVE_NATIVE_TASK=/Users/oleksii.naboichenko/.local/share/codex-local-worker/core-fixwave-2026-10-25/final/native-pilot/task.json \
python3 -m unittest tests.live.test_live_qualification -v
```

The new simulator was `4D3B2328-F90D-4703-9D83-956C7AFE5083`; both previously booted tvOS
simulators remained booted and untouched. All ten reports/worktrees across both post-fix live
runs, both native fixtures, simulator identity, and run outcomes are listed in the external
manifest at `/Users/oleksii.naboichenko/.local/share/codex-local-worker/core-fixwave-2026-10-25/live-job-manifest.json`.
The manifest and complete job state remain available for parent review and archive verification.
