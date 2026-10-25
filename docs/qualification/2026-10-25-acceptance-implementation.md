# Trusted acceptance checks implementation notes

## Interface

Write contracts may opt in with an absolute `acceptance_checks` manifest path. The v1 manifest shape is:

```json
{
  "version": 1,
  "checks": [
    {"name": "unit", "argv": ["python3", "/opt/project-checks/unit.py"],
     "scripts": ["/opt/project-checks/unit.py"]}
  ]
}
```

Each declared script must be an absolute regular file outside the repository, launcher state, report directory, worktree, and job Codex home. It must also appear as a standalone argv item. Manifest and script final components cannot be symlinks. The launcher canonicalizes file paths, reads the manifest from the same bytes it hashes, and pins SHA-256 plus device/inode identity.

The launcher adds all pinned inputs to the existing Seatbelt `deny_write_subpaths` policy. The job is blocked before worker launch if `--no-verify` is selected, if the deny wrapper is unavailable or cannot be applied, or if the manifest/scripts cannot be pinned. The report contains the full contract and `acceptance_checks` with manifest digest, per-input path/hash/device/inode, check results, and status. `planner_json` carries the same acceptance result. Checks use `run_verification`, its configured backend, the existing wrapper, and the shared `Deadline`; results are appended to attempt tests so ordinary local retry feedback sees failures. Inputs are checked before and after each check and after the full pass.

For integration-time validation, load the manifest again, compare its `Manifest` against the original `input_pins` with `acceptance_checks.matches_reported_pins`, then call `local_worker.run_acceptance_checks` with a live applied `DenyWrapper`. The helper blocks if that wrapper is absent. This binding protects acceptance-check identity across the review pause.

## Red/green evidence

Before the implementation module existed, the new focused test failed at import with `ModuleNotFoundError: No module named 'acceptance_checks'`, as expected. After implementation, the focused acceptance suite passed: 6 tests, covering contract opt-in/round-trip and unchanged legacy parsing, protected paths, changed-script detection, worker-writable-path rejection, bounded verifier failure evidence, and refusal without an applied wrapper.

`tests/test_contract_v2.py` also passed: 6 tests. `tests/test_permission_wrapper.py` was started as a related compatibility check; its output showed the existing wrapped-job tests passing through `test_without_sandbox_exec_jobs_run_and_say_so`, but the command output did not include a final suite summary in this run. The parent owns final suite execution.

## Limits

The check wrapper is the existing permissive Seatbelt deny profile; it is not a sandbox and provides no confinement beyond the explicit denials. This implementation adds a real Seatbelt profile path and fails closed when the wrapper is not applied, but the new focused run used an applied-wrapper test double for command routing and did not perform a fresh real-Sandbox-exec run specifically for external acceptance files. Existing `test_permission_wrapper.py` contains real wrapper integration coverage for the common profile behavior. Script inputs must be explicitly declared; the launcher does not inspect arbitrary command semantics to discover hidden script dependencies. Cloud semantic review remains a separate required gate.

## Parent integration verification

The parent added regressions for relative/symlink manifest rejection, no-verify refusal before
worker execution, and a real external acceptance command driving two worker attempts under the
shared deadline. Only inference is replaced with a fixture worker in that test; the verifier and
Seatbelt commands execute. The focused acceptance suite passed 9 tests. Workflow tests additionally
exercise actual primary-write refusal and rerun pinned acceptance checks after integration.
These results supersede the initial focused-wrapper test-double limitation above; live model
quality and matched token savings remain unmeasured.
