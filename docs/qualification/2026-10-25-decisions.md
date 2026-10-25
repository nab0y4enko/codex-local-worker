# Continuation decisions

This records the original R1–R25 rulings and every recovered continuation `Ruling:` entry in
ledger order. Later entries may explicitly supersede earlier choices. The original R1–R25 ledger
is the git-ignored `.superpowers/sdd/2026-10-24-universal-local-worker/progress.md`; it was not
found when this continuation first resumed, was located later, and its `Ruling` lines are imported
verbatim in the section below.
The user requested Luna subagents, complete implementation, real local-model tests,
analysis and fixes, and reviewed commits pushed to the task branch with verified cleanup.

- Ruling: Use gpt-6-luna subagents for implementation and review — direct user request supersedes historical Sonnet R21 and local-worker routing for this continuation — model capability may require narrower fix scopes.

- Ruling: Reconstruct missing ledger from committed handoff and history — ignored artifacts are absent — historical rulings remain unavailable. (Superseded: the original ledger was later found; see the imported R1–R25 section.)

- Ruling: Reuse primary checkout on universal-local-worker — explicit user completion instructions forbid extra routine worktrees — serial implementation preserves unrelated work.

- Ruling: Do not squash already published T2 history — origin/universal-local-worker already contains 4a2a8e5 despite stale handoff — rewriting would require a force push, explicitly unauthorized.

- Ruling: Commit/push reviewed results under current user-wide completion instructions — current direct instructions supersede handoff request to ask before pushing — no merge or force push authorized.

- Ruling: T12 fast redaction must preserve cross-chunk secret matches despite plan prefix-replace wording — spec secret confidentiality binds — use boundary-aware chunk replacement and regression test.

- Ruling: Cloud-auth spike uses synthetic throwaway homes and no live cloud inference — handoff forbids touching real Codex home and user specifically requires real local tests — real cloud token refresh remains outside live qualification evidence.

- Ruling: T17 must implement usable runtime cloud authentication, not leave empty job homes — ready-tool objective and spec6.8 require working escalation — implement isolated auth-only copy with guarded refresh handling (or equivalently robust auth-only strategy), verify synthetic source homes; no real credential reads/inference during this development spike. Costs if wrong: auth isolation/refresh rework, not silent unusable cloud feature.

- Ruling: Classifier ignores arbitrary raw stderr; actual CLI diagnostic records must be promoted explicitly to typed evidence — spec evidence boundary overrides plan raw-tail fixture examples — costs if wrong missed diagnostics require parser correction.

- Ruling: Replace auth-copy/writeback strategy with spec alternate real cloud CODEX_HOME and narrow auth-only read exceptions, Codex owns refresh/locking — no filesystem CAS against unrelated external writers is reliable — costs if wrong auth lock exceptions/ephemeral paths need correction; synthetic-only development, never touch actual home now. This supersedes previous copy-strategy choice.

- Ruling: Cloud profile may read literal CODEX_HOME directory metadata (read* literal only if CLI requires it), plus auth.json and observed tmp/arg0-macos subtree — generated profile startup otherwise fails — directory listing exposure if read* needed, child contents stay denied; validate synthetic config/history denial.

- Ruling: macOS native gate requires positive observed PID matching CFBundleExecutable in fresh built app, not successful open exit alone — actual launch evidence required by design — costs if wrong process matching may need platform-specific correction.

- Ruling: T23 initialcommit precedes gitarchiveHEAD installcheck — otherwise script tests oldmanifest rather than newlayout — correctiveamend remains reviewed, costs ifwrong installproof could target wrongsnapshot (verifyactualHEAD).

- Ruling: Correct fabricated package author/repository/license to verified nab0y4enko GitHub provenance; no MIT grant invented (omit optional license or UNLICENSED) — existing manifest had none — costs if wrong metadata needs owner correction.

- Ruling: Universal package version0.3.0 in sole manifest source — breaking0.2.0layout/behavior needs distinct installedcache version — costs if wrong downstream version expectations must update.

- Ruling: Read jobs use ONE Codex native named profile extending :read-only with explicit denylist/state protection; remove outerwrapper ONLY around trustedCodexreadparent, retain separate stdioMCP protection and read audit — exactjobprofile nestingfails sandbox_apply; installed0.164strictconfig/scriptedrealexecprovedallowedread/deniedsecret/view_image/worktree+primarywrite — costifwrong profilecomposition/compatibilityneedsfix, strictconfigmustfailclosed. Restore--output-schema forALLreadtiers; priorgrammarhypothesisdisproved. Bothlocal/cloudreadneed composition, cloudinference stillunauthorized. Officialsource https://learn.chatgpt.com/docs/config-file/config-reference.

- Ruling: Replace worker shared-object writes with isolated job object storage reading primary as readonlyalternate — primary protection must includehistory/objectdatabase, notmerelyfiles/refs — costsifwrong Git/audit/archive integration/performance needsrework; privateobjectvalidationmustpreventshadowingprimaryoids andparentimportsneveroverwriteexistingobjects. This supersedes deliberateoldsharedobjectswritability.

## Original rulings R1–R25 (from the task ledger)

Verbatim `Ruling` lines from the git-ignored task ledger, in order.

- Ruling R1: the controller created branch `universal-local-worker` before Task 1; Task 1's Step 1 is already done — the user's rules forbid a worktree when no other session works in the repo — cost if wrong: none (branch name per plan).

- Ruling R2: Task 4 also updates `plugin_setup.receipt_issue` and its tests to the new preflight receipt (`endpoint`, `profile.model.api_model` present, `model_fallback` false, `context_window` int) — keeps `plugin_setup.py --preflight` working between T4 and T23 — cost if wrong: small rework in T23/T24.

- Ruling R3: in Task 4 the I/O chunk literal `65536` in local_worker.py becomes a named constant `READ_CHUNK_BYTES = 1 << 16`; the plan's test stays as written — the test's intent is "no model context constant" — cost if wrong: none.

- Ruling R4: Task 15 adds attempt key `error_text` (redacted, ≤ 2000 chars) collected from typed `error` items and `turn.failed`/`error` event messages in the worker stream, and the classifier reads it — the plan's CASES rely on it — cost if wrong: classifier misses context/memory errors until fixed.

- Ruling R5: Task 13 creates `src/read_answers.py` holding only `SUPPORTED_SCHEMA_KEYWORDS`; Task 14 adds the rest to the same file — resolves the forward import — cost if wrong: none.

- Ruling R6: Task 17's auth spike never copies, prints, or moves `~/.codex/auth.json`; option (a) is dropped; only option (b) is evaluated (deny `~/.codex` except read access to what login needs), verified with `codex login status`; no real cloud model call (user asked for local testing) — copying credentials is security-sensitive — cost if wrong: cloud jobs keep auth readable by the worker, documented.

- Ruling R7: Task 27 prepares the widget spike fully, but installing it into the user's real Codex desktop (writes ~/.codex/config.toml) and judging the rendered widget need the user — stop and ask at that point — side effect outside the worktree — cost if wrong: a short pause.

- Ruling R8: Task 20 may inspect only key names/structure of real Codex rollouts (never message or tool content) to find the parent link — privacy — cost if wrong: subagent tokens reported as "not linked".

- Ruling R9: "Real tasks" in Task 26 = (a) read job on a real repository on this Mac (this repo itself), (b) write jobs on throwaway pilot repos, (c) a native capture/write job only if a real Xcode project and plan are available locally; otherwise the native part is reported as not run with the reason — the plan makes native live tests opt-in — cost if wrong: native path qualified later by the user.

Task 2: implementer DONE (169aa88, c97a255 scrub); Ruling: commit 169aa88 still contains a scratch path with the user name in history — squash Task 2 commits before any push (finishing step) — cost if wrong: username fragment in public history

Ruling R10: missing/blank server.host stays settings_invalid (real oMLX settings always carry host; global constraint allows only the port default) — cost if wrong: hand-written settings without host are refused with a clear message

Ruling R11: the 900 s timeout for the blocking POST /v1/models/{id}/load is accepted (only used by explicit --load); fix round adds a test pinning load=900 s, others=15 s — cost if wrong: a hung load blocks up to 15 min

Ruling R12: model candidates filter on model_type (plan/fixtures), not engine_type as spec §6.1.4 says — spec wording to be aligned in T25 docs — cost if wrong: none (fixtures prove model_type discriminates)

Task 5: implementer DONE (058c1a5). Ruling R13: the plan's example swap to two_loaded cannot change the pinned model (Qwen stays loaded); tests use none_loaded and a pinned-Example→one swap instead — the plan's test was wrong about fixture contents — cost if wrong: none

Ruling R14: accept the template (tool_mode direct, use_responses_lite false, apply_patch_tool_type null, local base instructions steering `apply_patch` heredoc via exec_command) although the brief's literal Step 2 asked for a patch tool in the tool list — oMLX 0.7.3 drops non-function tools, so the spec's intent (edits applied by Codex's patch engine) is met through interception — cost if wrong: switch to template-freeform (one field, also passed 4/4)

Ruling R15: Task 7 also (a) softens the template's edit rule so project formatters/generators/`git mv` are allowed when the task asks, adds `*** Move to:` to the patch format, keeps apply_patch heredoc as the way to hand-edit, and avoids conflicting with --output-schema final answers; then re-runs the spike's live gate for the template (≥3 pilot runs) to confirm no regression; (b) adds a "Limits" paragraph + instruction hashes to the spike record; (c) fixes spec §8 ("prompt-input shows tools" is wrong) and §6.2 ("patch tool" field) wording; (d) makes the spike script not double-add model_catalog_json once worker_argv does — the reviewer flagged untested-wording risk on real tasks — cost if wrong: one more live gate run

Ruling R16: Task 9 extends the wrapper so a worker cannot write the state directory (permissions.json/.log, reports, ledger, leases, other jobs) — deny file-write* (subpath <state>) then re-allow only this job's worktree, its codex-home, its attempt last-message files, its native scratch and screenshots dir, and $TMPDIR — the spec says reports/logs must not be worker-writable and changes to permissions are the user's; under (allow default) a worker could otherwise lift its own deny list for later jobs — cost if wrong: a legitimate worker write into the state dir is denied and classified with the exact path

Ruling R17: fix the rename bypass in Task 8 — the profile also denies writes to every hidden real path (subpath) and literal writes to every ancestor of each hidden path and of R plus R/.git and R/.git/refs — the spec's §6.2 guarantees ("cannot read credentials", "cannot change the user's checkout or branches") outrank the plan's rule list; build it as a reusable helper so Task 9's state-dir protection (R16) gets the same ancestor treatment — cost if wrong: a worker cannot rename/delete ancestor directories of hidden paths (e.g. cannot `mv ~/Library`), which no legitimate task needs

Ruling R18: Task 9 also closes two security gaps the re-review found outside the fix diff — (a) clonefile/fclonefileat of an ancestor directory copies hidden files out: add `(deny file-clone …)` for every ancestor of every hidden path and every hidden subpath (reviewer-tested remedy); (b) a worker can plant hooks in the primary checkout (R/.git/hooks, R/.git/info, R/.git/objects/info/alternates) that later run unsandboxed when the user runs git: deny writes there — plus fix `_path_end` over-capture (BSD mv "rename X to Y" etc. now returns trailing words) with tests, and document that missing ancestors of hidden paths cannot be created — the spec's §6.2 guarantees outrank the plan's rule list — cost if wrong: a tool that needs to create e.g. ~/.docker inside a job gets EPERM with the path named

Ruling R19: add a user-editable `deny_write` list to permissions.json (schema stays v1-compatible: missing key → defaults) with persistence vectors as defaults — ~/.gitconfig, ~/.config/git, ~/.zshrc, ~/.zprofile, ~/.zshenv, ~/.zlogin, ~/.bashrc, ~/.bash_profile, ~/.profile, ~/Library/LaunchAgents — plus primary-.git tightening: deny writes to R/.git/refs (all), R/.git/logs, R/.git/worktrees except this job's own entry — a worker could otherwise plant code that runs outside the wrapper later; reversible by the user via the permissions CLI — cost if wrong: a task that legitimately edits shell rc or global git config is refused with the path named

Ruling R20 (T10): the active record's `worker_command` is the unwrapped argv[0] (sandbox-exec execs in place, so `ps -o command=` shows the target; checked live: `/usr/bin/sandbox-exec -p ... /bin/sleep 3` → ps shows `/bin/sleep 3`); the reaper also matches a command starting with `/usr/bin/sandbox-exec` (the pre-exec window); the record follows whichever launcher child group is current (worker, verification command, native step) via `process_started`, and is removed in `run_job`'s finally — why: T9 wraps every step, and a single static pgid would miss verification/native orphans — cost if wrong: an orphaned verification step survives a launcher crash until the user kills it

Ruling R21: the user is low on tokens and asked for Sonnet — every subagent from here runs on sonnet (implementers, reviewers, final review); remaining tasks run in batches (T10–12, T13–14, T15–17, T18–19, T20–22, T23–25, T26, T27–28) with one implementer and one combined reviewer per batch, fix rounds resume the implementer; the opus T9 re-review was stopped and re-dispatched on sonnet — why: the user's explicit instruction; batching cuts the per-task dispatch overhead — cost if wrong: a weaker review misses a defect the final review then has to catch

Ruling R22: protect the resolved common dir (`GIT_COMMON_DIR` from worktree_git_layout) and, for a linked-worktree primary, the primary's own admin dir — never a hardcoded `<repo>/.git` — and inside the common dir switch to an allowlist: deny every write except `objects/` (minus `objects/info`) and this job's own `worktrees/<name>` admin dir (the job worktree is detached, so it needs no refs) — why: universal across separate-git-dir and linked-worktree primaries, and it closes MERGE_HEAD/index.lock/shallow/alternates in one rule instead of a growing denylist — cost if wrong: a git feature that writes elsewhere in the common dir during a worktree commit (gc --auto, packed-refs) warns or fails inside the job

Ruling R23: the local-model catalog moves out of the worker-writable codex-home into the report dir (readable, not writable by the worker); one `O_NOFOLLOW|O_NONBLOCK` regular-file read helper serves the -o last-message file and any other job file the launcher reads; a missing -o file is `last_message_missing`, not tampering — why: removes the hash oracle and FIFO hang at the root — cost if wrong: none known; the catalog path changes in reports

Ruling R24: the launcher pre-creates apple-scratch and screenshots before wrapping; check_conflicts also refuses a deny_write entry that covers the state dir, report dir, worktree or codex-home; the audit checks the redirect fingerprint before each git call and skips git on a mismatch; GIT_ADMIN_REDIRECTS lives in one module; the audit excludes base-tree gitlinks (status --ignore-submodules=all, pathspec exclusion for add) — why: each is a small fix in code this round already touches — cost if wrong: a gitlink path changed by the worker is not reported in the diff

Ruling R25: root timestamp side effect (NB4) is deferred to the final review — why: minor, and fixing it needs a finer Seatbelt operation split — cost if wrong: `touch .`/`tar -C .` inside the worktree root fails

## Scope deferral

- Ruling: Tasks 27 (MCP Apps dashboard spike/desktop gate) and 28 (dashboard widget) are deferred to future work, not part of this release — user decision 2026-10-25 — the v0.1.2 spike passed protocol tests and isolated CLI app-server startup but desktop rendering was never observed (Computer Use policy blocks driving the Codex app), and no temporary plugin is installed now — cost if wrong: no dashboard in this release; the CLI is unaffected.
