# Apple native verification and screenshots

Use a native plan when a job must build, install, launch, test, or capture an Apple app. Simulator
metadata from doctor is informational; only the plan's successful build/install/launch/test gate
qualifies the environment for a worker attempt. Native jobs use the launcher's full-host access;
the selected simulator and contract scope are not a security boundary.

## Prepare the native plan

Pass a v2 plan in the task contract's `native_plan` field, or pass its path with
`--native-preflight-plan` and select a simulator with `--simulator-udid <UUID>`. Use
`--preflight-only` to run the gate without starting a worker. Version 1 simulator plans remain
supported and are upgraded when loaded.

A v2 plan contains `version`, `destination`, `build`, `launch`, `test`, `app_bundle`,
`result_bundle`, and `bundle_id`; optional fields include `install`, `screenshots`,
`capture_sources`, and `capture_overlay_paths`. `destination` is `{"simulator":"<UDID>"}` or
`{"macos":true}`. Commands are argv arrays, not shell strings. A simulator plan's Xcode build
and test commands must bind `-destination` to that same UDID. Use a fresh `.app` and `.xcresult`
path inside the worktree or report-owned `apple-scratch` directory.

```json
{
  "version": 2,
  "destination": {"simulator": "11111111-2222-3333-4444-555555555555"},
  "build": ["xcodebuild", "-scheme", "App", "-destination", "id={simulator_udid}", "build"],
  "install": ["xcrun", "simctl", "install", "{simulator_udid}", "{app_bundle}"],
  "launch": ["xcrun", "simctl", "launch", "{simulator_udid}", "{bundle_id}"],
  "test": ["xcodebuild", "-scheme", "App", "-destination", "id={simulator_udid}",
           "-resultBundlePath", "{result_bundle}", "test"],
  "app_bundle": "{report_dir}/apple-scratch/App.app",
  "result_bundle": "{report_dir}/apple-scratch/native.xcresult",
  "bundle_id": "com.example.app"
}
```

The example shows the required structure; replace the scheme, app product, bundle identifier,
destination, and build settings with values from the target repository. Supported placeholders
include `{worktree}`, `{report_dir}`, `{simulator_udid}`, `{app_bundle}`, `{result_bundle}`, and
`{bundle_id}`. Relative paths are worktree-relative. Symlinks, traversal, protected metadata, and
pre-existing artifacts are refused.

## Gate evidence

The launcher runs the planned build, install, launch, and positive test in order, using the same
selected destination and full-host environment. The launch must return the planned bundle ID and a
positive process ID. The result bundle must include at least one passing test with no failures.
Each step records its command, status, duration, and artifact path. Missing dependencies, invalid
plans, unavailable devices, missing artifacts, failed commands, and commands that could not run
block the worker; they are not application-test failures.

For UI changes, set `ui_review` and declare screenshot steps in the plan. The launcher can capture
before and after images and write `screenshots/index.md`; named screenshot outputs are included in
the report. Review each image alongside the patch and test results. A `kind: "capture"` job can
capture a requested source without asking a worker to modify code.

## Diagnose the first failing stage

- **Plan validation:** check exact fields, argv arrays, placeholders, destination, fresh artifact
  paths, and bundle identifier.
- **Device/toolchain:** use doctor and the report's selected device and toolchain checks. Device
  availability is not proof that the build or test will pass.
- **Build:** inspect the recorded command and log. A missing dependency cache is a preparation
  blocker; do not mislabel it as a source regression.
- **Install or launch:** verify the prior artifact exists and that the command uses the selected
  device UUID and planned bundle ID.
- **Test:** inspect the result bundle summary and test counts. A build, runner PID, or simulator
  listing alone does not prove tests ran.
- **Screenshots:** inspect `screenshots/index.md` and the reported image paths. A missing before
  image must include its reason; do not infer that the UI stayed unchanged.

The task contract and `allowed_paths` scope the request; a full-host worker can still reach other
host files and tools. Keep secrets outside the repository and do not boot, erase, or operate on a
device other than the selected destination. See the [operating policy](operating-policy.md) for
leases and recovery.
