# tvOS simulator verification

Read for tvOS Xcode and simulator jobs. This backend qualifies tvOS simulators; it does not
qualify iOS simulators or physical devices. The skill's `preflight` command validates only
local model routing. Native jobs run on the same full-host backend as every other job: the
worker and the launcher's verification commands keep their own full filesystem and network
permissions, and no confinement claim is made about either. The real toolchain (`xcrun`,
`simctl`, `xcodebuild`, `xcresulttool`) is reachable by both. The gate below is positive
build/install/launch/XCTest proof, not a confinement probe; native tasks require it before
any Qwen attempt.

## Native preflight plan

Pass both `--apple-simulator-udid <UUID>` and
`--native-preflight-plan /absolute/path/plan.json` to `run`, or pass them with
`--preflight-only` to exercise the gate without launching a worker. The plan qualifies the environment before implementation. Keep the task contract’s
post-worker verification commands meaningful for the actual change; a successful preflight
does not verify a later patch.

Version 1 plan shape:

```json
{
  "version": 1,
  "build": ["xcodebuild", "-scheme", "MyApp", "-configuration", "Debug", "-sdk", "appletvsimulator", "-derivedDataPath", "{report_dir}/apple-scratch/DerivedData", "-destination", "id={simulator_udid}", "build"],
  "install": ["xcrun", "simctl", "install", "{simulator_udid}", "{app_bundle}"],
  "launch": ["xcrun", "simctl", "launch", "{simulator_udid}", "{bundle_id}"],
  "test": ["xcodebuild", "-scheme", "MyApp", "-sdk", "appletvsimulator", "-destination", "id={simulator_udid}", "-derivedDataPath", "{report_dir}/apple-scratch/DerivedData", "-resultBundlePath", "{result_bundle}", "test"],
  "app_bundle": "{report_dir}/apple-scratch/DerivedData/Build/Products/Debug-appletvsimulator/MyApp.app",
  "result_bundle": "{report_dir}/apple-scratch/native-preflight.xcresult",
  "bundle_id": "com.example.app"
}
```

The object has exactly these eight fields. Each of `build`, `install`, `launch`, and `test`
is a required, nonempty argv array, not a shell string. The supported substitutions are
`{worktree}`, `{report_dir}`, `{simulator_udid}`, `{app_bundle}`, `{result_bundle}`, and
`{bundle_id}`. Artifact paths must be fresh and resolve inside the worktree or
`{report_dir}/apple-scratch`; use scratch by default. Worktree artifact paths must already
be gitignored so generated products do not violate the patch scope; never widen allowed_paths
just for build output. Relative paths are resolved from the worktree. Use the project's
actual scheme, configuration, test target, and bundle identifier. The selected simulator's
runtime family must be tvOS. Build and test may specify the `appletvsimulator` SDK; both
commands must bind their `-destination` to the selected simulator UUID. Use the actual
scheme's app product path for `app_bundle` and a new `.xcresult` path for `result_bundle`.

The gate runs in this order, with the same full-host environment, toolchain, and resource
lease used by later native verification:

1. Run the direct `xcodebuild` build action from the job worktree and produce a fresh app
   bundle whose `Info.plist` bundle identifier matches the plan.
2. Install that app on the selected simulator.
3. Launch it by the planned bundle identifier.
4. Run the direct `xcodebuild test` action against that same simulator, writing its result
   bundle under report-owned scratch or an already gitignored worktree path. The result summary must show at least one passing test, no
   failures, and evidence for the selected device.

Install and launch must be direct `xcrun simctl install` and `xcrun simctl launch` commands
bound to the selected UUID and the planned app/bundle identifier. Launch must return the
planned bundle identifier with a positive process ID. Every stage must pass before Qwen
starts. The report retains each command, duration, exit status, and artifact path. A missing cache, unavailable device,
invalid plan, missing app product, launch error, command that could not run, or failed test
blocks the worker launch. Simulator availability metadata alone is not qualification; only a
passing gate is. `--preflight-only` is useful for preparing and reviewing this evidence before a worker
attempt. A canonical simulator UUID looks like
`8528DEBF-9328-4966-993B-D2230B96FB6C`; hardware identifiers such as
`00008101-001234567890001E` are not simulator UUIDs.

## Diagnose the first failing stage

- **Plan validation:** confirm exact schema, array arguments, placeholders, app path,
  bundle identifier, simulator UUID, and result-bundle location.
- **Gate availability:** `apple.checks[]` reports the selected device and toolchain
  discovery. Runtime state comes from `xcrun simctl list devices -j` on the host, so
  `isAvailable` and `state` are current rather than a fixed runtime-version table; read the
  JSON from stdout only, never from stderr. Availability is not qualification.
- **Build:** inspect the command's status and log. A cold SwiftPM or DerivedData cache is a
  preparation gap, not a refusal; prepare caches before the gate (see the cache procedure in
  [operating policy](operating-policy.md)).
- **Install or launch:** confirm the prior stage's artifact and the exact selected UUID.
- **XCTest:** confirm the result bundle and the `xcresulttool` test summary. Only that
  run's exit status and summary prove native tests executed; a green build alone does not.

Full-host execution means the worker can drive the simulator itself when its contract asks:
`simctl`, `xcodebuild`, and `xcresulttool` work directly against the one simulator UUID
scoped for the job. The task contract and `allowed_paths` are the boundary, not a sandbox
profile. `--no-verify` means the planned native commands were not run and cannot satisfy
acceptance.

## Failures are classified from diagnostics

Full-host execution removes the launcher-imposed sandbox, approval and execpolicy
restrictions; it does not remove host OS permissions or privacy controls, so a genuine
permission denial remains possible. Classify a failure from its actual diagnostics -- the
failing stage with its command, exit status and log -- rather than assuming every failure is
an application defect or that a permission denial is impossible. Report the evidence you
observed, and never claim a gate or test passed when a command could not run.

## XCTest runner startup and developer services

Capture the run's staged `Session-*.log` diagnostics before changing anything. A spawned
process can remain stopped at dyld while the harness waits for its startup handshake; a PID
is not evidence that any XCTest case ran. Use a bounded diagnostic run instead of repeating
the default 300-second startup wait for every bundle.

Native verification forces `IBToolNeverDeque=1`. The installed actool/ibtool supports this
mode; it prevents reuse of pooled ibtoold processes that cannot write the next job's fresh
DerivedData path. Do not share an app product to hide this failure. The report records the
setting, and `HOME` and `CODEX_HOME` remain unchanged. Qualify repeat fresh builds, not only
the first warm run.

## Historical: confined Seatbelt backend

The `apple-seatbelt` backend remains selectable only programmatically; no CLI flag exposes
it, and no current job uses it. The details below exist solely to interpret older reports and
evidence. Do not generate, grant, or widen Seatbelt rules for new jobs.

That legacy profile denied network by default and added narrow grants: `network-outbound` to
one exact literal Unix socket named `com.apple.testmanagerd.unix-domain.socket` under a
private `/private/var/tmp/com.apple.launchd.<alphanumeric>/` directory owned by the current
uid with no group or other permissions, discovered through
`xcrun simctl getenv <UUID> TESTMANAGERD_SIM_SOCK`; pseudo-tty allocation and data writes to
the literal `/dev/ptmx`, with slave writes and mutating ioctls allowed only when BOTH the
anchored path `^/dev/ttys[0-9]+$` and the macOS-issued `com.apple.sandbox.pty` runtime
extension matched; and three exact Mach names --
`com.apple.dt.instruments.dtarbiter.xpc`, `com.apple.dt.instruments.dtsecurity.xpc` and
`com.apple.testmanagerd.control` -- without widening the single-component `com.apple.dt`
pattern or adding job creation, Mach registration, or TCP/UDP.

Its readiness probe reported toolchain metadata, device visibility and confinement only; it
never compiled, installed, launched, booted, or ran XCTest, so probe success was never
qualification. Changing that profile historically required a live
`sandbox-exec -p <profile> /usr/bin/true` syntax check plus positive and negative socket,
network and PTY checks, followed by a complete build/install/launch/XCTest gate under the
same profile. See the [operating policy](operating-policy.md) for the legacy workspace-write
backend.
