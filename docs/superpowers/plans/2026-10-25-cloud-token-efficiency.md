# Cloud Token Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce cloud coordination while retaining all correctness checks and explicit cloud semantic review.
**Architecture:** Reuse run_job and its bounded retry/verification loop; add small evidence, workflow, and benchmark modules. Automation ends at review and resumes only on a digest-bound explicit decision.
**Tech Stack:** Python 3.9, standard library, unittest.
**Spec:** docs/superpowers/specs/2026-10-25-cloud-token-efficiency-design.md

## Global Constraints
- Current branch only; no development worktree; Luna subagents.
- Python 3.9 and standard library only.
- Keep permissions, audit, citations, native/UI checks and cloud semantic review.
- Preserve unknown usage as unknown; no claimed savings without matched measurements.

## Review Focus
- Tampered trusted check scripts or manifests must block acceptance.
- Stale review, changed checkout or patch must block integration.
- Missing, failed, skipped or truncated evidence must never look complete.
- Failed integration tests preserve changes/evidence and never auto-archive success.
- Matched benchmark includes retries and refuses mismatched or incomplete arms.

### Task 1: Trusted acceptance checks and local feedback
Files: src/contract.py, src/local_worker.py, new src/acceptance_checks.py, tests/test_acceptance_checks.py.
- [x] Write failing tests for opt-in trusted check manifest validation, tampering, failure feedback, and unchanged legacy contracts.
- [x] Add acceptance_checks manifest path to write contracts; pin manifest/scripts outside worker write scope, enforce protection, run through existing verification, retain results and bounded retries.
- [x] Verify focused tests and existing contract/worker tests. Record red/green evidence.

### Task 2: Review package and deterministic orchestration
Files: new src/review_workflow.py, tests/test_review_workflow.py; CLI integration in src/local_worker.py.
- [x] Test complete and incomplete evidence, patch/base/source binding, sequential stop-on-failure queue.
- [x] Add orchestrate and review-package commands; delegate worker execution to existing run_job or run CLI, no fresh agent supervisor.
- [x] Add accept/reject command requiring explicit cloud semantic review decision with matching package digest; verify checkout/patch, integrate, run combined checks, archive and summarize.
- [x] Test stale decisions, failed checks, retained dirty work and archive failure.

### Task 3: Benchmark accounting
Files: new src/benchmark.py, tests/test_benchmark.py, examples/benchmark.json.
- [x] Test matched task/acceptance identity, incomplete usage, retries, zero denominators and mismatched quality gates.
- [x] Implement comparison CLI with independent cloud cached/uncached/output totals, elapsed time and accepted-task outcomes. Optional direct Luna arm. No inference calls.
- [x] Verify focused tests and command example.

### Task 4: Documentation, integration and independent review
Files: README.md, skills/local-worker/SKILL.md, examples and qualification documentation.
- [x] Document conditional read, workflow, trusted check boundary, semantic decision and benchmark usage.
- [x] Run new real filesystem/git integration tests and complete offline suite once integrated.
- [x] Luna independent review; address reproduced findings and rerun affected tests.
- [x] Record exact results and remaining live qualification limits; no unmeasured savings claim.

## Execution record

User explicitly approved implementing all six improvements with Luna subagents on the current
branch; no new development worktree was created. Separate Luna implementers handled trusted
acceptance and benchmark accounting. Parent implemented workflow/CLI, conditional read guidance,
and duplicate-key diagnostics. Luna reviewers inspected acceptance inputs, workflow gates, and
the complete change. Review findings were corrected and regression-tested before final suite qualification.

Focused checks: acceptance 9 passed; workflow 22 passed; contract 13 passed; static test review
15 passed; benchmark 15 passed; classifier/planner compatibility 19 passed. Workflow includes a queue-to-decision fixture using real launcher reports, real Git
integration/archive, and actual bounded verification; only worker inference is simulated.

Ruling: automatic integration requires an enforceable, probed permission wrapper and keeps
primary source read-only during checks. This preserves reviewed content; build outputs must be
placed outside primary source. Missing protection fails closed and preserves evidence.

Ruling: package captures the final verified candidate fingerprint, all read citations/audit,
and original trusted-input identities. Missing fingerprints on historical reports are incomplete,
not silently upgraded to new verification evidence.

Final full offline suite: 1,148 tests run, eight live-only skips, zero failures, 382.176 seconds.
Source/test hashes unchanged during the run; no warnings. CLI help, example JSON parsing and
whitespace checks pass. See ../../qualification/2026-10-25-cloud-token-efficiency.md for evidence
paths, review corrections, and the explicit remaining matched-live-benchmark limitation.
