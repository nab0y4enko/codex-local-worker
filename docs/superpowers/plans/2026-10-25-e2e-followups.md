# Remaining CLI qualification fixes

Use the current `universal-local-worker` checkout; no planner worktree. Python 3.9 and
standard library only. Preserve exact citation verification, existing evidence and secrets
boundaries. No push without approval. The user has authorized further tests, including live
qualification, until the identified issues are addressed.

1. Supply a real read-contract example, test it with the production parser, and link it from
   planner instructions. Include required `goal`; do not introduce unsupported read fields.
2. Trace citation failures against the saved run. Add a reproducible way to obtain source line
   ranges and exact quotes, and instruct workers to use it. Keep invented/mismatched citations
   rejected. Any citation helper must enforce worktree/scope containment and bounded reads.
3. Prevent the concrete missing-assertion pattern from being silently accepted. Prefer a small,
   tested Python diagnostic plus explicit planner semantic review over pretending a general
   static scan proves arbitrary tests correct. Preserve support for other languages.
4. Mark planner summary usage as a snapshot until completion is evidenced. Re-running summary
   after the planner exits must refresh the final counts without another model call. Never
   infer final counts, fill missing values with zero, or label active usage complete.
5. Keep idle-state archive sequencing, whitespace normalization, and planner edit disclosure in
   the workflow. Test the final installed flow with isolated state/home and retained receipts.
6. For every behavior change, first add and run a failing regression test, then implement and
   run focused tests. Review changes independently with Luna agents, run the full offline suite,
   then run isolated live/CLI qualification. Record each failed and successful attempt. Fix
   reproducible product issues, rerun affected tests, and state any remaining reliability limits.
7. Commit small reviewed Conventional Commits without attribution; preserve all user evidence.
   Record final results and source commits, leave branch unpushed and ask about pushing.
