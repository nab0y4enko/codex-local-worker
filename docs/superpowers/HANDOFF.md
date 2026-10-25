# Handoff: universal local worker (2026-10-25)

## Where things are
- Branch `universal-local-worker` (local only, nothing pushed). Base `codex/unify-local-worker` @ dc88cad.
- Spec: `docs/superpowers/specs/2026-10-24-universal-local-worker-design.md`
- Plan: `docs/superpowers/plans/2026-10-24-universal-local-worker.md` (28 tasks)
- Ledger (git-ignored, the recovery map): `.superpowers/sdd/2026-10-24-universal-local-worker/progress.md`
  holds the pre-flight table, rulings R1–R25, per-task status and deferred minors. Task briefs
  `task-N-brief.md` for every task are already extracted there, plus reports and review files.
- Done and reviewed: Tasks 1–8. Task 9: main 193c26f and fix round 1 8d122af. The full suite was 892 OK, and the live wrapper smoke passed.
- In flight when this was written: Task 9 fix round 2, a Sonnet implementer working on rulings
  R22–R24 (protect the resolved git common dir, a safe job-file read helper, moving the catalog to
  the report dir, pre-creating apple-scratch and screenshots, deny_write conflicts, gitlink
  exclusion). Its findings are in `task-9-review-round1.md`, and it appends to `task-9-report.md`.
  - **Update:** fix round 2 landed as 7a87b53. The full suite is 939 OK, and the live smoke passed.
    Its concerns are logged in `task-9-report.md`: harmless `packed-refs.lock` noise on worktree
    commits, `objects/` stays writable, and gitlink changes are unreported.
  - **First step on resume:** run a scoped Sonnet re-review of 8d122af..7a87b53 against
    `task-9-review-round1.md`. If it is clean, add `Task 9: complete` to the ledger and start the T10–12 batch.

## How to finish
Process: superpowers:subagent-driven-development. All subagents run on Sonnet (ruling R21).
- **Batches:** T10–12, T13–14, T15–17, T18–19, T20–22, T23–25, T26, T27–28.
- **Per batch:** one implementer reads its `task-N-brief.md` files, then one combined reviewer. Fix rounds resume the implementer.
- **Faster mode:** skip the per-batch reviewer and rely on the final review.
- **Tests:** `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`. Python 3.9 stdlib only.
- **Rulings already made for upcoming tasks:**
  - R20 (T10): the reaper matches the unwrapped command and follows the current child group.
  - Carry items: grep "Carry to" in the ledger (T11 bounds `--load` by the deadline, T12 cleans codex-home, T15 classification hints, T21 notifies when the deny list is not applied, T25 fixes the stale "unwrapped" docs, doctor checks the wrapper).
- **Stop and ask the user:**
  - Before installing the dashboard into the real Codex desktop (R7).
  - Before any push.
- **Finish:**
  - Final whole-branch review including the deferred minors (R25 root timestamps; out-of-scope items in `task-9-review-round1.md`).
  - Squash T2 commits 169aa88 and c97a255 before any push.
  - List every `Ruling:` line in the final summary.
  - Then superpowers:finishing-a-development-branch.

## Standing rules
- **Commits:** no AI attribution; use Conventional Commits.
- **Secrets:** never print the oMLX API key; never read `~/.ssh`, the real git config or `~/.omlx/settings.json` values.
- **Live Codex runs:** use a throwaway CODEX_HOME, make no cloud model calls, and don't touch `~/.codex`.
- **Completion rule:** the user's commit/push/worktree rule lives in their own `~/.codex/AGENTS.md`. The plugin does not manage it.
