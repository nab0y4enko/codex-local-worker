# Continuation decisions

This records every recovered `Ruling:` entry in ledger order. Later entries may explicitly
supersede earlier choices. The original ignored R1–R25 ledger was absent when this
continuation resumed; its original wording is unavailable and has not been reconstructed.
The user requested Luna subagents, complete implementation, real local-model tests,
analysis and fixes, and reviewed commits pushed to the task branch with verified cleanup.

- Ruling: Use gpt-6-luna subagents for implementation and review — direct user request supersedes historical Sonnet R21 and local-worker routing for this continuation — model capability may require narrower fix scopes.

- Ruling: Reconstruct missing ledger from committed handoff and history — ignored artifacts are absent — historical rulings remain unavailable.

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
