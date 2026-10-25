# Cloud auth handling (synthetic fixture only)

## Result

Cloud jobs use an isolated, job-scoped `CODEX_HOME` containing only a safe copy of the selected
Codex home’s regular `auth.json`. No other Codex-home files are copied, and `permissions.json`
is never edited. The launcher holds a state-local lock per source home, copies the auth file with
private permissions, and records the original content hash. If the CLI refreshes credentials,
the launcher captures new secret values for redaction and writes the refreshed file back through
an atomic replacement only while the source still matches that hash. Symlinks, non-regular
files, invalid JSON, and changed source files fail closed or refuse writeback. Retained text
evidence is scrubbed with newly observed token values before the final report is written.

## Evidence

- A temporary directory outside the checkout held a synthetic user Codex home and a fabricated
  JWT-shaped `auth.json` containing only synthetic token strings.
- `codex login status` ran with both `HOME` and `CODEX_HOME` set to those temporary paths and
  exited 0 with `Logged in using ChatGPT`.
- An initial malformed synthetic ID token exited 1 with `invalid ID token format`, confirming
  that the CLI read and parsed the fixture at the isolated home.
- Synthetic runtime tests verify only `auth.json` is copied, copied mode is `0600`, token values
  are redacted from reports and retained logs, simulated refreshed tokens are written back, and
  a changed source, active concurrent launcher, or source/destination symlink is refused.
- No cloud request or real-home read was made during this qualification. The spike used only
  synthetic credentials and a synthetic source home.

## Limits and follow-up

This proves synthetic `codex login status` works with an isolated home, while unit tests prove the
launcher’s copy, refresh writeback, conflict refusal, and redaction behavior. It does not prove
that a real credential is valid or that a live Codex cloud request refreshes credentials as
expected. The synthetic JWT is not usable. No actual account auth or cloud call was used as part
of this implementation evidence.
