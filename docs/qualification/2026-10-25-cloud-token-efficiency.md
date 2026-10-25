# Cloud token efficiency implementation qualification

Implemented on the existing `universal-local-worker` branch with Luna implementation and
review subagents. No development worktree, commit, push, or merge was created for this change.
Usage and CLI examples are in [the workflow guide](cloud-token-workflow.md).

## Delivered behavior

- `orchestrate` validates and snapshots a sequential queue, reuses the original launcher budgets,
  and stops on failure or incomplete evidence. It returns compact review references.
- `review-package` binds the complete bounded diff, source, report, checks, citations, audit,
  screenshots, trusted inputs, and verified candidate to a digest. Missing or truncated evidence
  remains incomplete; historical reports are not silently upgraded.
- Independent acceptance manifests and scripts live outside worker-writable locations, are
  fingerprinted and protected, and run through the existing verifier and deadline. Failures feed
  the existing bounded local repair loop. Trusted tests are never rewritten as a repair.
- `decide` requires explicit cloud semantic review of the exact evidence. Acceptance applies the
  reviewed patch bytes, reruns original, additional, and trusted checks with primary source
  protected, then uses the existing archive and summary machinery. Failed integration retains
  applied changes and evidence. Passing tests never supply semantic acceptance.
- `benchmark` compares matched specifications and final usage, including failures, retries, and
  planner review. It distinguishes cached input, uncached input, and output. Acceptance requires
  actual integration and archive receipts linked to the counted report. Incomplete usage or
  unmatched evidence prevents a savings claim. Direct Luna is an optional comparison arm.
- Documentation makes preliminary read jobs conditional. Static test review additionally flags
  overwritten literal dictionary keys. Token placement is explicitly distinguished from savings.

## Verification and review

Focused checks passed: acceptance 9, workflow 22, benchmark 15, classifier/planner compatibility
19, contract 13, and static test review 15. The workflow fixtures use real Git integration,
archival, bounded verification, and Seatbelt enforcement; worker inference is simulated.
The acceptance fixture exercises a failing independent check followed by local repair under the
same job deadline. Other cases cover stale evidence, patch replacement between check and apply,
dirty user edits, protected source, missing wrappers, and failed post-integration checks.

Luna review findings led to enforceable integration protection, rerun trusted checks, pinned queue
contracts, timeout receipts, complete read evidence, and stronger benchmark receipt validation.
Path normalization now matches the launcher's declared-script argument normalization. A full
intermediate run exposed the legacy planner-response shape regression; the fix keeps the new
acceptance field opt-in, with the original stable-key regression unchanged.

Final verification: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`
completed successfully: **1,148 tests run, eight live-only skips, zero failures**, 382.176 seconds
(unittest time; 382.43 seconds including process startup). Source and test SHA-256 hashes were
unchanged throughout the run. No warnings were emitted. All four new CLI help routes succeeded,
all four example JSON files parsed, and `git diff --check` passed.

Evidence directory:
`/Users/oleksii.naboichenko/.local/share/codex-local-worker/efficiency-implementation-2026-10-25/`.
`offline.log` and `offline-receipt.json` contain the final output and per-file source hashes.
`initial-offline.*` retain the full 1,146-test run with the planner-shape failure.
`interrupted-offline.*` retain an explicitly interrupted run superseded by the path-normalization
fix and final run; it is not used as passing evidence.

## Remaining qualification limits

No new live model run or matched direct-cloud/local-worker benchmark was performed for this
implementation. Offline correctness results do not establish measured cloud-token savings or
local model success rates. The eight live qualification tests require explicit live execution.

All existing permission, audit, citation, native/UI, and cloud semantic-review gates remain.
Automatic integration requires an enforceable permission wrapper; tests needing build outputs
inside the primary checkout must be configured to use external scratch. Trusted dependencies
must be declared; their protection does not establish the sufficiency of the tests. Semantic
review fields are caller attestations, not authentication of a cloud model identity.
