# Handoff: fix the end-to-end findings (2026-10-25)

Paste this file's path to the next agent: "Read docs/superpowers/HANDOFF-e2e-fixes.md and do it."

## Context

- Repo: `codex-local-worker`, branch `universal-local-worker` (plugin 0.3.0, Python 3.9 stdlib only).
  A Codex plugin: the cloud model in Codex plans and reviews, `bin/local-worker` runs the model
  loaded in oMLX (127.0.0.1:8000) as a sandboxed coding worker.
- State: all core tasks done; dashboard Tasks 27–28 deferred to future work. Offline suite:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests` → 1032 OK, 8 live-only skips
  (~7 min).
- First real end-to-end run through the Codex CLI passed with issues:
  [docs/qualification/2026-10-25-e2e-cli.md](../qualification/2026-10-25-e2e-cli.md) — read it
  first. Evidence (event stream, reports, archives): `~/.local/share/codex-local-worker/e2e-2026-10-25/`.
- Other records: `docs/qualification/2026-10-25-second-model.md` (loaded model
  `Qwen3.5-9B-OptiQ-4bit`, read-job `--output-schema` fix), `2026-10-25-readiness.md`,
  `2026-10-25-decisions.md`, and the general handoff `docs/superpowers/HANDOFF.md`.

## Fix these (from the e2e run)

1. **Lock contention is misclassified.** A job refused because another worker holds
   `worker.lock` reports `verification_blocked` / class `environment` / `needs_user` with the
   remediation "make the verification command runnable". It should get its own typed status
   (e.g. `worker_busy`, retryable, next action "wait and retry, or pass `--lock-timeout`"), and
   must not say the job was verified or blocked by verification. Test first.
2. **Serialization is undocumented.** `skills/local-worker/SKILL.md` and README never say local
   jobs run one at a time, nor mention `--lock-timeout` / `--max-workers`. Document it, so a
   planner queues jobs (or uses `--lock-timeout`) instead of launching them in parallel.
3. **Worktrees are left behind.** The skill (around `skills/local-worker/SKILL.md:83`) says to
   archive only after reviewed work is committed and pushed, and the summary step doesn't mention
   archiving, so a planner that integrates patches without committing leaves job worktrees
   registered. Decide and document a flow where every finished job is archived once its result
   is integrated (or rejected), commit or not; consider having `summary` list unarchived jobs.
4. **Summary tier totals show "not reported"** when a never-started (blocked) job has no usage,
   because the sum in `src/ledger.py` `build_summary` is all-or-nothing. Exclude jobs that never
   started (no worker attempt) from the totals while keeping "missing is not zero" for jobs that
   ran but reported no usage. Test first.
5. **Patch whitespace (minor).** Worker patches carry trailing whitespace, so a plain `git apply`
   warns. Check whether the launcher should run `git diff --check` and surface it, or document
   `--whitespace=fix`.

Not a plugin bug, just note it: the planner edited one line of worker output without saying so;
the skill could require the planner to disclose edits it makes to worker results.

## Then verify

- Full offline suite OK, no new warnings.
- Re-run the CLI end-to-end once, the same way as the e2e record (throwaway `CODEX_HOME` with the
  plugin installed via `codex plugin marketplace add <checkout>` +
  `codex plugin add codex-local-worker@codex-local-worker`, `plugin_setup.py --apply`, a small
  Python fixture repo, one prompt asking for two features using local workers,
  `codex exec --json -s danger-full-access`). Cloud model calls are approved for this re-run only.
  Copy `~/.codex/auth.json` with `install -m 600` without reading it and delete the copy after.
- Expect: no misclassified refusal, jobs serialized or waited properly, every job worktree
  archived by the planner's flow, summary totals populated, token table local vs cloud.
  Record results in a new `docs/qualification/2026-10-2x-e2e-cli-rerun.md` and link it from
  `docs/qualification/README.md`. Report honestly; a skipped step or manual help is not success.

## Rules

- Conventional Commits, small commits, no AI/Claude attribution or Co-Authored-By lines.
- Do not push without asking the user; never force-push or rewrite pushed history.
- Never print or read the oMLX API key, `~/.omlx/settings.json` values, `~/.ssh`, or the real git
  config. Never modify `~/.codex`; use throwaway Codex homes. No cloud calls beyond the approved
  re-run. Do not write secrets to disk (an earlier experiment left a key copy in a world-readable
  settings file; it was deleted).
- Do not delete user evidence, reports or archives under `~/.local/share/codex-local-worker/`.
