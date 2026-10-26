# Single Cloud Workflow Implementation Plan

> For agentic workers: execute through the pinned local-worker launcher; the current cloud session performs final semantic review.

**Goal:** Make the successful single-cloud/single-local topology the ordinary addon workflow without weakening acceptance.
**Architecture:** Reuse deterministic orchestration and the canonical review package/decision pipeline; add a single-contract entry point, an integrity-checked readable handoff and pre-inference guards.
**Tech Stack:** Python 3.9+, standard library; unittest with fake workers and disposable Git repositories.
**Spec:** docs/superpowers/specs/2026-10-25-single-cloud-workflow-design.md

## Global constraints
- Same branch and primary checkout; implementation/test authoring by the pinned local worker only.
- One coherent local assignment; no cloud subagents or live benchmark; no disabled checks.
- Preserve existing verification, trust, scope, native, digest, retry, integration and archive semantics.

## Review focus
- Duplicate or cloud-tier tasks must be refused before any inference.
- A previously accepted dirty target must not incur another implementation call.
- Human-readable source escapes and Markdown fences must preserve exact source bytes.
- A tampered view, canonical package, patch, source or trusted check must block acceptance.
- Legacy manifest queues and canonical-only historical packages must retain supported behavior.

## Task 1: Default single-session handoff
Files: src/review_workflow.py, tests/test_review_workflow.py, README.md, docs/qualification/cloud-token-workflow.md, skills/local-worker/SKILL.md.
- [x] Add meaningful failing offline regressions for single-task entry, local-only/duplicate/dirty-target validation, readable complete source/check evidence and tampered-view refusal.
- [x] Observe expected failures before implementing.
- [x] Reuse queue internals for --task-file; generate and deterministically verify the readable review view while retaining full canonical evidence and all acceptance gates.
- [x] Document the current cloud caller as planner/reviewer, one coherent local job, no model polling/preparation or mandatory second reviewer. Keep the current global policy/migration input unchanged.
- [x] Run targeted workflow/benchmark regressions and the full offline suite; inspect diff and retained red/green evidence.
- [x] Current cloud planner reviewed and integrated the exact accepted patch; the full offline suite passed (1,163 tests, 8 skipped). Commit/push and archive cleanup follow as the parent completion actions.

## Review adjustment
The source package intentionally does not ship a global-policy.md (verified by its unchanged packaging regression). The worker drafted one assuming the old installed 0.2 layout; that file was excluded from integration and retained only in the worker archive. Default workflow guidance lives in the shipped skill.
