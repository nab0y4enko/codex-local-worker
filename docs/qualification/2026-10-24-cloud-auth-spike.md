# Cloud auth handling (synthetic fixture only)

## Result

Cloud jobs use the selected native `CODEX_HOME` so the Codex CLI owns authentication, refresh,
and any credential locking. The job profile denies reads of that home except read-metadata on the
home directory itself, the literal `auth.json`, and the observed `tmp/arg0-macos` helper subtree.
The metadata exception lets Codex inspect its home directory at startup; it does not reopen child
file contents or permit directory listing. It allows writes only to `auth.json` and that
temporary helper subtree; the rest of the home remains hidden and write-denied. The cloud worker
also uses `--ignore-user-config` and `--ignore-rules`. The launcher does not copy or write
credentials, and it never edits
`permissions.json`. It reads auth values only into a private redaction set before execution and
again after execution to scrub retained text evidence if Codex refreshed tokens. Local jobs keep
their empty per-job Codex homes.

## Evidence

- A temporary directory outside the checkout held a synthetic user Codex home and a fabricated
  JWT-shaped `auth.json` containing only synthetic token strings.
- `codex login status` ran with both `HOME` and `CODEX_HOME` set to those temporary paths, under
  the generated deny profile, and exited 0 with `Logged in using ChatGPT`. A preceding run without
  the directory metadata exception failed to open `CODEX_HOME`; with the metadata-only exception,
  auth file and helper access worked. The Seatbelt regression confirms directory listing,
  `config.toml` reads, and history child reads remain denied.
- `codex exec --ignore-user-config --ignore-rules --ephemeral --json --help` exited 0 under the
  generated profile with synthetic `config.toml` and session history present but unreadable. This
  is an argument/config startup probe only; it did not start a worker turn or make a cloud call.
- The CLI created `CODEX_HOME/tmp/arg0-macos/<random>/.lock` and temporary `apply_patch` helpers.
  This supports the helper subtree exception; it did not create an auth-specific lock file.
- An initial malformed synthetic ID token exited 1 with `invalid ID token format`, confirming
  that the CLI read and parsed the fixture at the isolated home.
- Synthetic runtime tests verify the cloud worker points at the selected source home, its source
  auth changes only when the fake Codex process simulates a refresh, and old and refreshed token
  values are scrubbed from reports and retained logs. Profile tests verify auth and helper access
  while `config.toml`, session history, and other home file contents stay unreadable.
- No cloud request or real-home read was made during this qualification. The spike used only
  synthetic credentials and a synthetic source home.

## Limits and follow-up

This proves synthetic `codex login status` works with the selected native home while the rest of
the home is denied. The login-status command does not exercise a cloud request or token refresh;
the exact ChatGPT auth refresh lock/path therefore remains unverified. In particular, the observed
`.lock` is inside the CLI's temporary helper subtree, not an auth-specific lock. A live cloud
request was not made, and the synthetic JWT is not usable. No real home or credential was read
during implementation or testing.
