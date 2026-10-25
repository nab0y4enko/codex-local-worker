# Cloud token efficiency

Implement the six improvements approved in chat: deterministic orchestration, launcher-built review packages, independent acceptance checks, bounded automatic local repair, explicit revision-bound cloud review acceptance followed by integration/checks/archive/summary, and matched benchmark accounting. Make preliminary read jobs conditional.

Use the current universal-local-worker branch; no development worktree. Python 3.9 and standard library only. Preserve all existing permission, audit, source-citation, deadline, native/UI, and semantic-review requirements. Workers remain untrusted. Do not infer semantic acceptance from passing tests. No automatic cloud escalation or model calls for housekeeping.

## Approach

Extend the existing launcher and retry loop. Add focused modules for acceptance checks, review packaging/workflow, and benchmark comparison. The alternative of an agent supervisor adds model overhead; weakening or replacing cloud review is excluded. User authorized implementation of this approach.

Independent check definitions and scripts are supplied by the planner outside the worker checkout, fingerprinted before launch and checked before/after verification. Deny worker writes to these inputs when the existing wrapper supports it; fail closed on tampering or inability to enforce required protection. Execute through the existing bounded verification backend and shared deadline. Report checks separately and feed failures into the existing local retry budget; never rewrite trusted tests.

Orchestration runs a validated queue sequentially using existing run_job, stopping on failure and returning evidence without integration. One compact review package per result contains the complete patch or explicit incompleteness, actual verification and audit results, relevant bounded source context, hashes, and links to full evidence. The package always requires cloud semantic review.

A separate explicit cloud review decision binds package digest, patch digest, base commit and primary checkout state. Acceptance validates unchanged inputs and successful required checks, integrates only reviewed content, reruns combined checks, then archives using existing lifecycle validation and writes summary. Failure preserves evidence and reports any applied changes; never resets unrelated work or automatically rolls back user changes. Reject decisions archive without integration. All integration/archive operations use existing shared-pool exclusion where applicable.

Benchmark tooling compares recorded matched tasks with equal acceptance specifications and successful semantic review, includes failures/repairs, distinguishes cached and uncached cloud input and output, and refuses savings claims for missing/final-incomplete data. It does not claim measured savings until paired real runs exist. Direct Luna is an optional comparison arm, not a hard-coded production model.
