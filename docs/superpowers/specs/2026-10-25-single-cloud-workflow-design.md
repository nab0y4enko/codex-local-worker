# Single cloud planner/reviewer workflow

User-selected architecture: one existing cloud planner/reviewer, one bounded local implementation assignment, deterministic orchestration; preserve correctness checks and do not run another live benchmark.

The current queue runner and explicit decision boundary already perform deterministic verification, integration and archival. Improve the ordinary entry point and handoff rather than adding an agent supervisor. Add `orchestrate --task-file <absolute-contract>` as the normal single-assignment path while retaining explicit manifest queues. The caller plans and semantically reviews in its current cloud context; no preparation model, polling model, forced independent reviewer, or automatic cloud escalation. Related stages belong in one bounded contract and one worker context.

Render a readable review.md from the canonical digest-bound package: frozen task, complete diff, complete changed sources/tests, actual checks, findings, audit and screenshots/native evidence or explicit absence. Retain the full canonical JSON package, bindings, logs and original hashes. Validate the rendered view against a deterministic rendering of the canonical package before accepting/rejecting so edited or truncated views cannot mislead review. Existing semantic attestation remains required; the tool does not authenticate cloud session identity.

Prevalidate every queue contract before inference: local tier only in orchestrate, duplicate repository/task identities refused, write paths overlapping dirty primary files refused. Preserve explicit lower-level run/escalation interfaces and legacy valid manifest queues. Do not infer acceptance from successful CLI exit or passing tests.

All current trusted checks, permissions, scope/citation/native gates, retry/deadline budgets, stale evidence refusal, exact patch integration, integration reruns and verified archival remain in force. No savings claims without a future authorized measurement.

Alternatives: another cloud supervisor recreates observed overhead; removing semantic review weakens correctness. Neither fits the user-selected design.
