# Local Worker

Local Worker lets the model selected in the Codex window plan and review repository work while
the launcher runs scoped local jobs. It discovers the currently loaded local chat model and
endpoint at run time, creates an isolated Git worktree for each job, runs requested verification,
and returns a patch, audit, screenshots when configured, and structured usage evidence.

## Install

To install this checkout (including a review branch), add its directory as a local marketplace
and install the plugin:

```sh
codex plugin marketplace add /absolute/path/to/codex-local-worker
codex plugin add codex-local-worker@codex-local-worker
```

For the published default branch, use its GitHub marketplace source instead:

```sh
codex plugin marketplace add nab0y4enko/codex-local-worker
codex plugin add codex-local-worker@codex-local-worker
```

The first-party manifest is `.codex-plugin/plugin.json`; it declares skills from `./skills/`.
The installed root is client-managed. Do not assume a fixed cache path. Start with the bundled
setup and doctor skills, or run the installed launcher's read-only report.

```sh
<root>/bin/local-worker doctor --json
<root>/bin/local-worker preflight
<root>/bin/local-worker models --json
```

`doctor` reports the Codex version and required flags, local model discovery or an error code,
permissions mode and deny lists, wrapper availability, simulator metadata, state directory, and
manifest version. `preflight` checks the live endpoint and Codex compatibility. `models` shows
current local model IDs and limits; nothing in the docs assumes a particular model or port.

## Setup migration

For a repository checkout or after a manual package installation, inspect setup first:

```sh
python3 <root>/src/plugin_setup.py --check
```

`--check` does not write. If an older plugin block or exact legacy policy needs migration, an
authorized `--apply` preserves the user's completion rule outside plugin markers and backs up the
previous `AGENTS.md` before an atomic change. It does not add new global instructions by default.
`--apply --pointer` opts into two marker lines pointing at the skill and read-only doctor command.
See [the setup skill](skills/setup/SKILL.md) for override handling and the migration receipt.

## Job workflow

1. **Inspect.** Run doctor/preflight and use a read job when repository behavior needs evidence.
   Read jobs specify a question, allowed `scope_paths`, a JSON `answer_schema`, and a size limit;
   the launcher validates the answer and citations.
2. **Plan.** Split the request into small jobs with explicit acceptance criteria. Keep write-job
   `allowed_paths` narrow and non-overlapping. A job contract is JSON, and verification commands
   are argv arrays, never shell strings. The legacy nine-field write contract in
   [`examples/task.json`](examples/task.json) remains supported.
3. **Run.** Use `kind: "write"` for implementation, or `kind: "capture"` for a screenshot-only
   task. Run a write contract with:

   ```sh
   <root>/bin/local-worker run --task-file /absolute/path/task.json --json
   ```

4. **Review.** Inspect the returned `status`, `classification.next_action`, patch, tests, audit,
   logs, screenshots, timings, and token counts. A successful command is evidence for that
   command; it is not approval to merge a patch. Keep the main checkout and unrelated active
   work intact.
5. **Continue.** Follow the returned `next_action`. Retry local work only with the concrete
   remediation. If it says `escalate_cloud`, inspect the live catalog using
   `models --cloud --json`; select the cheapest suitable live entry and start with low reasoning
   effort. Continue the same job contract and link it with `--escalates <job-id>`.
6. **Summarize.** Run `<root>/bin/local-worker summary` when the session's jobs are complete. It
   writes Markdown and JSON summaries, sends a best-effort notification, and includes measured
   outcomes, model/tier totals, planner usage when available, screenshots, escalations, and
   **Suggested fixes for this plugin**. Missing measurements remain unavailable, not zero.

### UI work and screenshots

Provide a native plan with a write job when UI behavior needs simulator verification. The plan
names the destination, build/install/launch/test steps, app and result artifacts, and optional
screenshots. Set `ui_review` to capture before/after results when supported by the plan. Inspect
`screenshots/index.md` and every image with the code diff and XCTest summary; a device listing or
successful build alone does not qualify native tests. See
[Apple verification](skills/local-worker/references/apple-verification.md).

## Permissions

Jobs run with full-host access and the launcher's deny wrapper when available. Worktree isolation,
`allowed_paths`, and the audit detect scope problems after the job; they do not restrict every
filesystem or network access. Keep credentials outside repositories sent to workers.

`permissions.json` belongs to the user. Never edit it by hand. Use
`<root>/bin/local-worker permissions show` to inspect the effective list; explicit
`deny-read`, `allow-read`, `deny-write`, and `allow-write` commands are available when the user
requests a change. Doctor only reads an existing file and does not create missing permission
state.

Known limit: the wrapper write-protects the job worktree directory itself so it cannot be
renamed or replaced, so `touch .` (or `tar -C .` setting times) at the worktree root fails with
"Operation not permitted" inside a job. Files and subdirectories below the root are unaffected.
Re-allowing only `file-write-times` on the root literal was tried on 2026-10-25 and does not work:
with `(deny file-write* (literal <root>))` in place, no allow of any named `file-write-*` operation
(times, data, create, unlink, mode, owner, flags, xattr, setugid, acl), alone or together,
makes `touch <root>` succeed on this macOS; only a full `(allow file-write* ...)` does, which
would also re-open rename protection. Seatbelt does not offer the split, so this stays a known limit.

## Troubleshooting by classification code

Use the current report's `classification.code`, evidence, remediation, and `next_action`:

| Code | What to check |
| --- | --- |
| `server_unreachable` / `server_loading` | Check the local model service, then rerun doctor and preflight. |
| `model_not_loaded` / `model_ambiguous` / `model_changed` | Load or select a currently available chat model; do not guess an alias. |
| `missing_env` / `missing_tool` | Provide the named environment value or install the missing verification command. |
| `verification_blocked` | Make the exact verification command runnable; blocked is not a pass. |
| `simulator_unavailable` | Check the selected simulator's current state and lease. |
| `secret_read_denied` | Review the reported path; change the user's permission list only if needed and explicitly requested. |
| `stall` / `budget_exhausted` | Inspect event and timing evidence; narrow the job or adjust its total budget. |
| `tests_failed` / `out_of_scope` / `no_change` | Review the patch and audit; follow the proposed repair or live cloud escalation. |
| `invalid_answer` / `unverified_answer` | Fix the answer schema or citations before accepting a read result. |
| `policy_refused` / `deny_list_unavailable` | Read the refusal and doctor output; resolve the reported policy or wrapper issue before retrying. |

Do not infer success from a worker's prose, an empty log, a simulator's presence, or a command
that could not run. `status --state-dir <dir> [--job-id <id>]` reads live progress. Preserve failed
and interrupted reports, logs, worktrees, and patches for review. After the task branch is
committed and pushed, archive completed launcher worktrees and verify the receipt, recovery ref,
and bundle before cleanup. See [operating policy](skills/local-worker/references/operating-policy.md).

## Tests

Run the offline unit suite with:

```sh
python3 -m unittest discover -s tests -v
```

The suite uses local fixtures and fake model/tool responses; it does not require model inference.
