# Prompt: matched live comparison on another Mac

Paste the text below into a new Codex chat opened in this repository after checking out
`universal-local-worker` and pulling its latest changes. The fixture and independent probes are
committed under `examples/matched-benchmark/`; no evidence files from the original Mac are needed.

---

Run a real matched benchmark of this Local Worker implementation on this Mac. Execute the work,
retain raw evidence, and tell me whether local worker saves cloud tokens without reducing correctness.

Use the current branch. Do not create a development worktree, merge, push, or change production code
as part of the experiment. Isolated fixture repositories are appropriate; the launcher's own worker
worktrees are part of the product under test. Use Luna subagents for bounded setup/review assistance
when useful, and count their tokens in the appropriate arm or explicitly separate experiment setup
overhead. Do not send implementation work to uncounted agents.

## Frozen tasks and correctness

Use `examples/matched-benchmark/fixture/` as the pristine baseline. It contains the original textkit
core and tests, without the requested features. Use `slug-task.json` and `stats-task.json` beside it
as the exact task goals, criteria, allowed paths, and ordinary test commands. Each task is independent:
start every arm and repetition from the same pristine baseline, not the previous task's output.

Create one seed Git repository from the fixture and clone that seed for all arms. To reproduce its
commit on another Mac, use branch `main`, author and committer `Benchmark Fixture <benchmark@localhost>`,
author and committer date `2026-10-25T00:00:00+00:00`, commit message `Frozen textkit benchmark baseline`,
and identical tracked bytes/modes. Disable hooks and signing for this disposable seed only. Record
the full base commit and tree hashes; verify every candidate starts at them. Preserve fixture files.

Copy `check_acceptance.py` to a trusted directory outside every candidate repository and launcher
state. Freeze its SHA-256 before any inference. For each task create an acceptance manifest naming
the check and running `[concrete_python, absolute_check_script, "slug" or "stats"]`, with the script
listed in `scripts`. Use that same manifest and check bytes for all three arms. Add its absolute path
as `acceptance_checks` when expanding the task template with its candidate `repo_path`. Resolve one
concrete Python interpreter for every arm, and replace the template's `python3` consistently. Never
use Python optimization, which disables the probe assertions. Set `PYTHONDONTWRITEBYTECODE=1`.

Retain normal unittest discovery, independent probes, scope/dirty-file audits, static test review,
and cloud semantic review. The probes cover eight explicit cases plus 200 seeded probes per task;
stats also checks mutation independence. Pay attention to Unicode astral letters, punctuation,
underscores, apostrophes, and duplicate dictionary keys. Slug NFKD-to-ASCII conversion happens before
separator replacement, so `slugify("a—b") == "ab"`. Tests are evidence, not semantic approval.
Freeze checks before running any arm; do not change expectations after seeing outputs.

## Three arms

1. **direct_cloud**: direct Codex implementation with `gpt-6.1-sol`, low reasoning.
2. **launcher**: real `bin/local-worker orchestrate` using the discovered local model, followed by
   explicit `gpt-6.1-sol` low-reasoning semantic review and `bin/local-worker decide`. Use the new
   compact review package and deterministic coordination. No mandatory preliminary local read
   for these already-scoped tasks. No direct Codex bypass for local implementation.
3. **direct_luna**: direct Codex implementation with `gpt-6-luna`, low reasoning.

Verify those cloud model IDs in the live catalog. Use the same cloud reviewer model and reasoning
for every arm, with a fresh review context and the same review checklist. Direct arms are explicitly
authorized to use `codex exec`; the local-worker skill's launcher-only rule applies to the local
arm, not to these deliberate controls. Do not substitute cloud models silently. Record the local
model's canonical ID, quantization, actual context/output limits, thinking mode and server version.
The previous Mac had `Qwen3.5-9B-OptiQ-4bit`; if this differs, label the result as a different model
configuration, not a pure hardware comparison.

Use equivalent clean Codex homes/configuration for direct implementations and cloud reviewers;
retain authenticated access securely without printing or committing credentials. Keep permission
denials and checks intact. Record any unavoidable differences in tool catalogs or instructions.
Run discovery/doctor first; do not weaken a permission gate to obtain a passing run.

## Execution and repair budget

Run three paired repetitions per task and arm (18 candidates total), sequentially on the local
inference server. Rotate arm order across repetitions to reduce ordering effects. Never reuse an
implementation or reviewer context between arms, tasks, or repetitions. Do not preload answers.
Do not clear global caches or change the user's model-server settings; record cache state/usage.

Keep the template's 600-second job budget and at most two internal local attempts. Allow at most
one further implementation/semantic-repair round per arm, with at most 1,200 implementation seconds
total per task/arm/repetition. For direct arms use the same time budget and at most two implementation
turns; tests/tool retries inside those turns remain counted. Give each semantic review a 300-second
cap and at most two reviews per candidate sequence. Record these different native retry mechanisms
explicitly; do not reset deadlines or workstreams to conceal failures. Preserve failed/rejected work.
Do not escalate the local arm to cloud implementation or let the reviewer edit the candidate.

For every candidate run the ordinary and trusted checks. The reviewer must inspect the full diff,
source, tests/assertions, findings, audit, and applicable screenshot evidence, then explicitly accept
or reject. Review failing candidates too, without turning failure into acceptance. Local acceptance
must use a digest-bound decision and the normal integration/check/archive pipeline. For direct arms,
retain equivalent patch/source hashes, scope audit, protected check execution, and external semantic
acceptance evidence. Record review rejections and repairs. No screenshots/native checks apply to
this Python fixture; mark them not applicable rather than inventing proof.

Use the launcher workflow guide and `examples/benchmark.json` as the evidence schema. Count every
launcher job exactly once, including failed/rejected jobs and all attempts already aggregated in its
report. Keep a decision record for each job. If a failed job cannot produce complete acceptance
evidence, report the arm as incomplete/failed; do not forge a package or receipt to satisfy the tool.

## Token accounting

Launch measured model work in finite Codex sessions so final rollout usage is available after exit.
Retain raw JSONL events, final rollout counts, model IDs, commands, timings, prompts, reports,
packages, review decisions, check logs, integration receipts and archive proofs. Include implementation,
repair, semantic review, and any model-based coordination tokens. Deterministic shell work adds time,
not model tokens. Do not treat missing usage as zero or a snapshot as final.

Refresh real session summaries after the measured planner exits using `bin/local-worker summary`
with the correct session ID, state, and Codex home. A parent summary already includes discoverable
linked subagents: never add those tokens twice. If reviewer sessions are independently launched,
retain their raw final usage and transparently aggregate each session exactly once; document the
aggregation and reconcile it against raw events. Do not claim unlinked review usage was counted by
the automatic ledger. Direct arms have `reports: []`; their usage comes from real cloud rollouts.

Create matched benchmark manifests with frozen specification hashes, the correct trusted-check
semantic fingerprint, and every raw evidence reference. Run `bin/local-worker benchmark --manifest
<file> --output <result.json>`. Separate per-repetition identities as needed to satisfy unique task
IDs. If the comparator cannot represent a failed/incomplete arm, retain its refusal and report the
failure alongside measured usage; never omit unsuccessful runs to improve the apparent savings.

Report cached cloud input, uncached cloud input, output, total cloud input-plus-output, local tokens,
elapsed time, successful acceptance, retries, failed checks, semantic rejections and review calls.
Reasoning output is part of output, not an additional token bucket. Compare token categories
separately; cached-heavy totals are not dollar or subscription-limit savings. Attribute experiment
setup and this orchestration chat separately and disclose that overhead in the overall study cost.

## Deliverable

Save a portable evidence pack and a concise Markdown report with:
- machine/chip/RAM, OS, Python/Codex/server versions, repository commit and model settings;
- frozen fixture/check/spec hashes and the actual budget/configuration;
- all 18 outcomes, including failures, with separate implementation and review token totals;
- paired per-task/per-repetition comparisons and medians/ranges across repetitions;
- percentage change versus direct cloud and direct Luna for uncached input, cached input, output,
  and total cloud tokens, only where matched accepted evidence supports that comparison;
- wall-time comparisons and an explicit verdict: saves, does not save, or inconclusive for each
  baseline. With three repetitions this is a small pilot, not a general performance guarantee.

Keep results available for comparison with this same kit on another Mac. Do not compare against
the old qualification report as a matched baseline; it used a different orchestration workflow and
did not include direct control arms. Finish with the concrete result and evidence paths. Do not
stop at a proposed plan; run the authorized live benchmark unless an actual environmental blocker
prevents it, in which case retain partial measurements and state the exact blocker.
