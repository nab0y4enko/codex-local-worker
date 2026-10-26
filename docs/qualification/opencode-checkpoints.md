# OpenCode four-phase checkpoint helper

`scripts/opencode_checkpoint.py` is an **addon-free** helper around the **native OpenCode CLI**.
It runs exactly one checkpoint phase per invocation, in a **new** native OpenCode session, with an
explicit `--agent` and an explicit `--model`, and it saves complete raw evidence for that phase.

It is a **manually advanced four-phase helper, not an autonomous correctness controller.** It does
not chain phases, does not enforce scope, and does not enforce acceptance criteria. The caller
supplies the packet, decides when to advance to the next phase, and runs the real checks. Nothing
here proves a task was completed correctly.

## Contract

| Argument | Requirement |
|---|---|
| `--phase` | one of `discover`, `plan`, `implement`, `review` (required) |
| `--repo` | absolute path to an existing Git checkout; pinned as subprocess `cwd` **and** `env["PWD"]` |
| `--packet` | existing UTF-8 file; the full packet is sent with the phase instructions |
| `--output` | **fresh** evidence directory; an existing path is refused before any model starts |
| `--cloud-model` | explicit `provider/model#variant` |
| `--local-model` | explicit `provider/model` (no `#variant`) |
| `--timeout` | seconds, default `600`; must be a **finite** number greater than zero (`nan`, `inf`, `Infinity` are rejected, exit `2`, nothing launched) |
| `--opencode` | executable, default `opencode` |
| `--dry-run` | print effective routing/command metadata; no model call, no filesystem writes |

Routing is a fixed table lookup, so the same arguments always produce the same command:

| Phase | Agent | Model | Job |
|---|---|---|---|
| `discover` | `plan` | local | read-only evidence relevant to the packet; no delegation |
| `plan` | `plan` | cloud | short bounded implementation contract, no edits, no delegation, acceptance criteria preserved verbatim, ends `PLAN: READY` or `PLAN: BLOCKED: <reason>` |
| `implement` | `build` | local | only the assigned allowed paths; ordinary/trusted tests plus repair within the packet retry budget; no delegation, no commits; failure evidence preserved |
| `review` | `plan` | cloud | read-only semantic review of the complete supplied patch/source/tests; no delegation; ends `VERDICT: PASS` or `VERDICT: CHANGES_REQUIRED`; passing tests alone are insufficient |

Every stage launches:

```
opencode run --standalone --format json --agent <agent> --model <model> <phase instructions + full packet>
```

`--standalone` with no `--session`/`--continue` means a **new native session every phase**: each
cloud phase starts in a fresh cloud context, with no hidden carry-over from a previous checkpoint.
`cwd` **and** `PWD` are pinned to `--repo`, so the model sees the intended checkout. The helper
uses `argv` lists only (`shell=True` is never used), never autoapproves permissions, and never
writes global model/config/provider state: the model is passed per invocation, so your
`~/.config/opencode/opencode.json`, default agent and provider settings are untouched.

## The four checkpoint invocations

```bash
REPO=/absolute/path/to/checkout
PKT=$PWD/packets/task-1831.md
CLOUD=openai/gpt-6.1-sol#high          # selected cloud model is always explicit
LOCAL=omlx/qwen-3.8-nf-splash
EV=$PWD/evidence/task-1831

python3 scripts/opencode_checkpoint.py --phase discover  --repo "$REPO" --packet "$PKT" \
  --output "$EV/01-discover"  --cloud-model "$CLOUD" --local-model "$LOCAL"

# Read 01-discover/response.txt, then write the plan contract into the packet and advance by hand.
python3 scripts/opencode_checkpoint.py --phase plan      --repo "$REPO" --packet "$PKT" \
  --output "$EV/02-plan"      --cloud-model "$CLOUD" --local-model "$LOCAL"

# Confirm the plan response ends `PLAN: READY`; add allowed paths + retry budget to the packet.
python3 scripts/opencode_checkpoint.py --phase implement --repo "$REPO" --packet "$PKT" \
  --output "$EV/03-implement" --cloud-model "$CLOUD" --local-model "$LOCAL"

# Supply the complete patch, source and tests in the packet, then review in a fresh cloud context.
python3 scripts/opencode_checkpoint.py --phase review    --repo "$REPO" --packet "$PKT" \
  --output "$EV/04-review"    --cloud-model "$CLOUD" --local-model "$LOCAL"
```

Advance between phases yourself. **Phase switching is not invisibly inferred from the OpenCode TUI
model/agent picker:** the agent and model for a phase come only from this table and the explicit
`--cloud-model`/`--local-model` flags on the command line. Switching a picker in the TUI does not
change what this helper launches, and this helper does not read or imitate TUI state.

Preview without launching anything (`--dry-run` performs no subprocess, no model call and no
filesystem write, and does not dump packet contents):

```bash
python3 scripts/opencode_checkpoint.py --phase review --repo "$REPO" --packet "$PKT" \
  --output "$EV/04-review" --cloud-model "$CLOUD" --local-model "$LOCAL" --dry-run
```

## Evidence

The evidence directory is created with `exist_ok=False` **before any model starts**; an existing
path is refused (exit `2`) rather than reused or overwritten, and failed runs are retained, never
cleaned up.

| File | Contents |
|---|---|
| `prompt.txt` | the exact prompt: phase instructions plus the full packet |
| `events.jsonl` | raw stdout JSON events, streamed as they arrived, byte for byte |
| `stderr.log` | raw stderr (or the recorded launch error) |
| `response.txt` | concatenation of the **assistant text parts only**: native `{"type":"text","part":{"type":"text",…}}` envelopes and `message.part.updated` events whose `properties.part.type` is `text`. Tool inputs, outputs and metadata are never copied into the response |
| `execution.json` | phase, agent, model, tier, explicit cloud/local models, pinned `cwd`/`PWD`, full `argv`, start/finish timestamps, `elapsed_seconds`, `exit_code`, `timed_out`, `session_ids`, event counts, `error_events` (real failure envelopes only), `failure_reasons` |

**What the helper does and does not do about secrets.** It reads no credential from anywhere,
forwards no credential of its own, prints none, and never dumps the inherited
OpenCode/local-server environment (API keys, tokens, `Authorization` headers) into the evidence or
onto a command line. That is *not* a claim that the recorded `argv` cannot contain a secret: the
exact packet text is persisted **verbatim** in `prompt.txt`, forwarded as the last `argv` element,
and recorded verbatim in `execution.json["argv"]`. Anything you paste into the packet — or pass in
`--packet`, `--repo`, `--cloud-model`, `--local-model`, `--opencode` — is written to disk in the
evidence directory and stays there. **You must keep secrets out of the packet and the arguments**;
treat `prompt.txt`, `events.jsonl` and `execution.json` as sensitive artifacts, and do not commit or
share an evidence directory you have not read first.

The helper returns **nonzero** for: an argument/validation problem (`2`, nothing launched); a
nonzero model exit, an error event, an empty assistant response, a timeout, or a failed launch
(`1`). Every one of those paths keeps all raw files and writes `execution.json` with the reason
list. On timeout the whole child **process group** is terminated, with bounded escalation from
`SIGTERM` to `SIGKILL` if it will not stop, and the partial events already received are retained.
A non-finite `--timeout` (`nan`, `inf`, `Infinity`) is an argument problem: exit `2`, no evidence
directory, nothing launched, because such a deadline would either never fire or fire immediately.

## Evidence integrity: assistant text is not tool output

A real `tool_use` event legitimately nests `{"type":"text","text":"…"}` blocks inside
`part.state.output` and `part.state.metadata.content`, and a failing tool legitimately carries an
`error` object in the same place. The parser deliberately does **not** search events recursively:

* `response.txt` is built only from the two documented assistant text envelopes
  (`type=text` with `part.type=text`, and `message.part.updated` with
  `properties.part.type=text`). A `tool_use` event with nested `type=text` contributes **no**
  response text, so a run that only ran tools still fails with `no_assistant_text` instead of
  reporting the tool's stdout as the model's answer.
* `error_events` is populated only by a real top-level `{"type":"error",…}` event or by an
  `error` field on a message/session envelope (`message.updated`, `message.part.updated`,
  `session.error`). An `error` key inside tool input/output/metadata is data the assistant read,
  never a fatal provider failure, and never turns a successful phase into `error_event`.

Raw events are still saved byte for byte in `events.jsonl`, so nothing is lost — only the
inference is kept honest.

## Budget and stopping

Target: **0.30 x the matched cloud-only baseline** for the same task and acceptance requirements.
Count, once each, the native **cached input + uncached input** and, separately, **output** —
including every retry and repair turn across all four checkpoints. Use the native saved request
usage (per-session usage records/SQLite) externally; this helper computes no token estimates and no
prices. If usage accounting is incomplete or the matched baseline is not comparable, the budget
claim is unsupported.

There is **no guaranteed 70 percent saving** here. One matched pair is one data point, local model
quality and latency vary, and cached-input categories differ between providers. When the budget is
exceeded, a checkpoint returns failure, a check fails, or evidence is incomplete, **stop** and
report it. Do not relabel a failed or over-budget run as a pass, and do not accept a result whose
acceptance checks you did not run.

## Tests

`tests/test_opencode_checkpoint.py` uses a temporary fake executable only — no live models, no
provider requests, no installs. It covers deterministic phase-to-agent/model routing with no config
mutation, fresh-session arguments, pinned `cwd` and `PWD`, refusal of an existing `--output` before
launch, `--dry-run` performing no launch and no writes while hiding packet contents, error-event /
nonzero-exit / no-text / missing-executable failures with retained evidence, and timeout handling
with process-group termination and escalation to kill. Parser fixtures replay realistic native
events: a `tool_use` envelope whose `state.metadata.content` nests `{"type":"text","text":…}` and
whose `state.metadata.error` is set (asserting **no** assistant text is inferred, so the phase still
fails with `no_assistant_text`, and **no** `error_event` is inferred), the same nesting carried in a
`message.part.updated` tool part, real `type=text` / `message.part.updated` text still extracted
byte-for-byte beside that tool data, and a real top-level `{"type":"error",…}` provider failure that
still fails the checkpoint and is summarized. Argument validation is covered for relative or missing
`--repo`, a non-existent `--packet`, malformed model strings, and `--timeout` values that are zero,
negative, or non-finite (`nan`, `inf`, `Infinity`), each refused with exit `2` before any evidence
directory exists and before the fake executable is launched.

```bash
python3 -m unittest discover -s tests -p test_opencode_checkpoint.py -v
```

These offline tests establish helper behaviour. They do not measure cloud tokens, savings or model
quality; that needs real matched paired runs recorded separately.
