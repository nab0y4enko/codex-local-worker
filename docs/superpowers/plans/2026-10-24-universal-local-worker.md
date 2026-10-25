# Universal Local Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn codex-local-worker into a model-agnostic oMLX worker system: live model discovery,
read/write/capture jobs, failure classification with cloud escalation, secret-hiding full-host
workers, platform-generic native verification with screenshots, and a complete session report.

**Architecture:** The launcher stays a stdlib-Python CLI (`bin/local-worker`) that composes small
modules: discovery (oMLX), Codex invocation (generated model catalog, isolated home), a Seatbelt
deny wrapper, contract v2, verification/native/screenshots, classification, budgets, ledger and
summary. Existing working code (pool, audit, archive, tool manifests) is moved or adapted, not
rewritten. Each phase leaves the suite green and the CLI usable.

**Tech Stack:** Python 3.9 stdlib, `unittest`, git, Codex CLI ≥ 0.164.0, oMLX ≥ 0.7.3,
`/usr/bin/sandbox-exec`, `xcrun simctl`/`xcodebuild`/`xcresulttool`, `osascript`.

**Spec:** `docs/superpowers/specs/2026-10-24-universal-local-worker-design.md` — read it before
any task; section numbers below (§) refer to it.

## Global Constraints

- Python 3.9 stdlib only; tests run with `python3 -m unittest` (no pytest); macOS only.
- No model id, alias, context size, or per-attempt time cap in code. The only allowed fallback is
  oMLX's port `8000` when `settings.json` omits `server.port`, recorded as `port_source: "default"`.
- Model endpoint is loopback only (`127.0.0.1`, `localhost`, `::1`); any port.
- The oMLX API key and every passed-through or tool credential never appear in argv, prompts,
  logs, reports, notifications, or the ledger.
- Workers run with full host access by default, wrapped in the Seatbelt deny profile (§6.2, §6.14).
  The planner never edits `permissions.json`; the skill says so.
- Codex worker invocation keeps `--ignore-user-config`, `--ignore-rules`, `--json`, and the argv
  self-check; every expected value comes from the job's pinned profile.
- State root `~/.local-implement` and env names `LOCAL_WORKER_STATE_DIR`, `LOCAL_WORKER_CODEX_BIN`
  (legacy `LOCAL_IMPLEMENT_*` fallbacks) are unchanged.
- v1 nine-field contracts and v1 native plans keep working (upgraded on load).
- The user's commit/push/worktree-cleanup rule is never deleted by setup (§6.11).
- Work on branch `universal-local-worker` created from `codex/unify-local-worker`; one commit per
  task, Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`); no AI
  attribution lines in any commit.
- Modules stay flat under `src/` (the existing layout); the spec's §6 tree maps to:
  `omlx_discovery.py`, `codex_catalog.py`, `permissions.py`, `contract.py`, `read_answers.py`,
  `classify.py`, `ledger.py`, `notify.py`, `screenshots.py`, plus existing `local_worker.py`,
  `worker_observability.py` (runner + events), `native_readiness.py`, `worker_pool.py`,
  `run_policy.py`, `worktree_lifecycle.py`, `worker_tools.py`, `worker_tool_probe.py`,
  `plugin_setup.py`.

## Review Focus

- oMLX busy serving another client (oMLX chat UI, a second job): a worker waiting in the server
  queue emits no events; the stall watchdog must extend instead of killing it (Task 11 test
  `test_stall_extends_while_server_busy`).
- Repository or state paths with spaces, quotes or non-ASCII characters: the Seatbelt profile and
  every argv must stay valid (Task 8 test `test_profile_escapes_awkward_paths`).
- The user switches the loaded model in oMLX between preflight and an attempt: the attempt must not
  start and the job is `model_changed`, not a capability failure (Task 5 test
  `test_model_swapped_before_attempt_blocks_attempt`).
- The second Mac runs oMLX on another port or an older `settings.json` without `server.port`
  (Task 3 test `test_port_comes_from_settings_or_documented_default`).
- oMLX restarts or crashes during an attempt: the run is `server_unreachable` (environment), never
  `tests_failed` or an escalation (Task 15 test `test_server_crash_mid_attempt_is_environment`).

---

## Phase A — Baseline

### Task 1: Branch, baseline, remove the wrong context warning

The working tree holds uncommitted interim changes (configurable model/alias/context/attempt
flags, `tests/test_model_settings.py`). Their argument threading is reused; the
`sampling.max_context_window` comparison is wrong (§5) and goes.

**Files:**
- Modify: `src/local_worker.py` (delete `read_server_context_window`, the
  `server_max_context_window` / `context_window_warning` keys in `run_preflight`)
- Modify: `tests/test_model_settings.py` (delete `test_context_window_above_the_server_cap_is_reported`
  and the `server_cap` parameter of `write_unaliased`)
- Modify: `README.md` (drop the sentence about `context_window_warning` in "Choosing the model")
- Add: `docs/superpowers/specs/2026-10-24-universal-local-worker-design.md`,
  `docs/superpowers/plans/2026-10-24-universal-local-worker.md`

**Interfaces:**
- Produces: `config_overrides(alias, context_window)`, `missing_config_pins(argv, alias,
  context_window)`, `worker_argv(..., context_window=)`, `_policy_check(argv, alias,
  context_window)` (already in the tree) — later tasks extend these signatures.

- [ ] **Step 1:** `git switch -c universal-local-worker codex/unify-local-worker`
- [ ] **Step 2:** Make the deletions above.
- [ ] **Step 3:** Run `python3 -m unittest discover -s tests` — expected `OK` (517 minus the
  deleted test).
- [ ] **Step 4:** Commit:

```bash
git add -A src/local_worker.py tests/test_model_settings.py README.md skills/setup/SKILL.md docs/superpowers
git commit -m "feat: thread model, context and attempt settings through the worker pins"
```

### Task 2: Recorded oMLX and Codex fixtures

**Files:**
- Create: `scripts/capture_fixtures.py`
- Create: `tests/fixtures/omlx/{health_ok,health_loading,api_status,models_status_one,models_status_none_loaded,models_status_two_loaded,models_status_helper_and_embedding}.json`
- Create: `tests/fixtures/codex/{exec_pilot.jsonl,debug_models.json,rollout_planner.jsonl}`
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Produces: fixture files used by Tasks 3–5, 12, 15, 17, 20; helpers
  `tests/support.py::load_fixture(relative: str) -> Any` (JSON, or a list for `.jsonl`) and
  `load_fixture_text(relative: str) -> str`.

- [ ] **Step 1: Write the failing test** (`tests/test_fixtures.py`)

```python
class FixtureHygiene(unittest.TestCase):
    def test_fixtures_exist_and_hold_no_secrets(self):
        root = Path(__file__).parent / "fixtures"
        for rel in ("omlx/models_status_one.json", "omlx/api_status.json",
                    "codex/exec_pilot.jsonl", "codex/debug_models.json"):
            text = (root / rel).read_text()
            self.assertNotRegex(text, r"(?i)bearer\s+\S{8,}|api_key\"\s*:\s*\"[^\"<]")
            self.assertNotIn(os.path.expanduser("~"), text)

    def test_models_status_one_has_one_loaded_chat_model(self):
        status = sp.load_fixture("omlx/models_status_one.json")
        loaded = [m for m in status["models"] if m["loaded"]]
        self.assertEqual(len(loaded), 1)
        self.assertIn(loaded[0]["model_type"], ("llm", "vlm"))
        self.assertIsInstance(loaded[0]["max_context_window"], int)
```

- [ ] **Step 2:** `python3 -m unittest tests.test_fixtures -v` — FAIL (files missing).
- [ ] **Step 3: Implement `scripts/capture_fixtures.py --out tests/fixtures`**: reads
  `~/.omlx/settings.json` for port/key, GETs `/health`, `/api/status`, `/v1/models/status`, copies
  the pilot's `attempt-1.jsonl` (path argument) and `codex debug models` (trimmed to `slug`,
  `display_name`, `description`, `context_window`, `supported_reasoning_levels`,
  `default_reasoning_level`, `visibility`); replaces the home path with `~`, drops `api_key`,
  `secret_key`, `sub_keys`. Run it once against this Mac's oMLX. Write the synthetic variants by
  hand from the real file: `health_loading` (status `loading`, HTTP 503 recorded as
  `{"_http_status": 503, ...}`), none-loaded, two-loaded (second `llm` model), and
  helper+embedding (`is_helper: true` entry and an `embedding` entry, both loaded).
  `rollout_planner.jsonl`: a `session_meta` line with `id` = `"019a0000-0000-7000-8000-000000000001"`
  and three `event_msg`/`token_count` lines with increasing `total_token_usage`.
- [ ] **Step 4:** `python3 -m unittest tests.test_fixtures -v` — PASS.
- [ ] **Step 5:** Commit `test: add recorded oMLX and Codex fixtures`.

---

## Phase B — Discovery and worker invocation

### Task 3: oMLX discovery module

**Files:**
- Create: `src/omlx_discovery.py`
- Test: `tests/test_omlx_discovery.py`
- Modify: `tests/support.py` (add `FakeOmlx`)

**Interfaces:**
- Produces (all in `omlx_discovery`):
  - `CHAT_MODEL_TYPES = frozenset({"llm", "vlm"})`; `DEFAULT_PORT = 8000`
  - `@dataclass(frozen=True) ServerSettings(host: str, port: int, api_key: str = field(repr=False), model_fallback: Optional[bool], port_source: str)` with property `base_url -> str`
  - `@dataclass(frozen=True) ModelProfile(canonical_id: str, api_model: str, model_type: str, engine_type: Optional[str], context_window: int, max_output_tokens: Optional[int], accepts_images: bool, thinking_default: Optional[bool], reasoning_efforts: Optional[List[str]])`
  - `@dataclass(frozen=True) ServerProfile(base_url: str, version: Optional[str], model: ModelProfile, prefill_tps: Optional[float], generation_tps: Optional[float], server_prompt_tokens: Optional[int], active_requests: Optional[int], discovered_at: str)` with `as_report() -> Dict[str, Any]` and `effective_context(cap: Optional[int]) -> int`
  - `class DiscoveryError(Exception)` with attributes `code: str`, `candidates: Tuple[str, ...]`; codes: `settings_invalid`, `not_loopback`, `model_fallback_enabled`, `server_unreachable`, `server_loading`, `auth_failed`, `model_not_loaded`, `model_ambiguous`, `model_unknown`
  - `HttpFetch = Callable[[ServerSettings, str, bool], Tuple[int, Any]]`
  - `read_server_settings(path: Path) -> ServerSettings`
  - `http_fetch(settings: ServerSettings, path: str, auth: bool) -> Tuple[int, Any]` (no proxies, redirects refused, 15 s timeout)
  - `select_model(models_status: Dict, health: Dict, requested: Optional[str]) -> Dict`
  - `discover(settings_path: Path, requested_model: Optional[str] = None, fetch: HttpFetch = http_fetch) -> ServerProfile`
  - `recheck(profile: ServerProfile, settings_path: Path, fetch: HttpFetch = http_fetch) -> Optional[str]` (returns `None`, `"model_changed"` or `"server_unreachable"`)
  - `load_model(settings_path: Path, model_id: str, fetch: HttpFetch = http_fetch) -> None` (POST `/v1/models/{quote(id)}/load`)
- Produces (`tests/support.py`): `write_settings(path: Path, *, host: str = "127.0.0.1", port: Optional[int] = 8000, model_fallback: bool = False) -> Path` (`port=None` omits the key; key is `sp.SECRET`), and `FakeOmlx(variant: str = "one", overrides: Optional[Dict[str, Any]] = None)` — callable with the `HttpFetch` signature, serving `/health`, `/api/status`, `/v1/models/status` from the fixtures of Task 2; `.calls` records paths.

- [ ] **Step 1: Write the failing tests** (`tests/test_omlx_discovery.py`)

```python
class Discovery(unittest.TestCase):
    def test_single_loaded_model_is_selected_with_server_limits(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(profile.model.api_model, "Qwen3.5-9B-OptiQ-4bit")
        self.assertEqual(profile.model.context_window, 262144)
        self.assertEqual(profile.model.max_output_tokens, 32768)
        self.assertTrue(profile.model.accepts_images)
        self.assertNotIn("api_key", json.dumps(profile.as_report()))

    def test_alias_is_the_api_model(self):
        fake = sp.FakeOmlx("one", overrides={"model_alias": "nine-b"})
        self.assertEqual(d.discover(self.settings, fetch=fake).model.api_model, "nine-b")

    def test_helpers_and_embeddings_are_never_candidates(self):
        profile = d.discover(self.settings, fetch=sp.FakeOmlx("helper_and_embedding"))
        self.assertIn(profile.model.model_type, d.CHAT_MODEL_TYPES)

    def test_two_loaded_models_need_an_explicit_choice(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("two_loaded"))
        self.assertEqual(ctx.exception.code, "model_ambiguous")
        self.assertEqual(len(ctx.exception.candidates), 2)
        chosen = ctx.exception.candidates[1]
        self.assertEqual(d.discover(self.settings, chosen, fetch=sp.FakeOmlx("two_loaded")).model.canonical_id, chosen)

    def test_none_loaded_lists_what_could_be_loaded(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("none_loaded"))
        self.assertEqual(ctx.exception.code, "model_not_loaded")
        self.assertTrue(ctx.exception.candidates)

    def test_loading_server_and_fallback_are_refused(self):
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("one", overrides={"_health": "loading"}))
        self.assertEqual(ctx.exception.code, "server_loading")
        write_settings(self.settings, model_fallback=True)
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.discover(self.settings, fetch=sp.FakeOmlx("one"))
        self.assertEqual(ctx.exception.code, "model_fallback_enabled")

    def test_port_comes_from_settings_or_documented_default(self):
        write_settings(self.settings, port=8123)
        self.assertEqual(d.read_server_settings(self.settings).base_url, "http://127.0.0.1:8123")
        write_settings(self.settings, port=None)
        settings = d.read_server_settings(self.settings)
        self.assertEqual((settings.port, settings.port_source), (8000, "default"))

    def test_non_loopback_host_is_refused(self):
        write_settings(self.settings, host="0.0.0.0")
        with self.assertRaises(d.DiscoveryError) as ctx:
            d.read_server_settings(self.settings)
        self.assertEqual(ctx.exception.code, "not_loopback")
```

- [ ] **Step 2:** `python3 -m unittest tests.test_omlx_discovery -v` — FAIL (module missing).
- [ ] **Step 3: Implement `src/omlx_discovery.py`.** Selection (§6.1.4): candidates are entries
  with `loaded` true, `is_helper` falsy, `model_type` in `CHAT_MODEL_TYPES`; a requested name
  matches `id` or `model_alias`; then single candidate; then `health.default_model` if a
  candidate; else `model_ambiguous`. `accepts_images = model_type == "vlm" or engine_type == "vlm"`.
  `prefill_tps` is reported only when `/api/status.total_prompt_tokens >= 10000` (smaller samples
  are meaningless), else `None`. `model_fallback` must be exactly `false`.
- [ ] **Step 4:** `python3 -m unittest tests.test_omlx_discovery -v` — PASS.
- [ ] **Step 5:** Commit `feat: discover the loaded oMLX model and its limits`.

### Task 4: Launcher uses discovery (preflight, pins, report, CLI)

**Files:**
- Modify: `src/local_worker.py` — `run_preflight`, `validate_endpoint`, `ENDPOINT_RE`,
  `config_overrides`, `missing_config_pins`, `worker_argv`, `JobOptions`, `new_report`, `_execute`,
  `build_parser`, `main`, `summary_lines`; delete `CANONICAL_MODEL`, `EXPECTED_ALIAS`,
  `MODEL_CONTEXT_WINDOW`, `read_model_alias`, `read_model_fallback`, `fetch_model_ids`,
  `resolve_model_settings`, `add_model_arguments` and the four `LOCAL_WORKER_*` model env names.
- Modify: `tests/support.py` (`job_options` takes `omlx=FakeOmlx(...)`; `write_settings` writes
  only `settings.json`), every test that used `lw.EXPECTED_ALIAS`/`lw.CANONICAL_MODEL`/
  `models_available` (`test_contract_policy`, `test_full_host_access`, `test_review_repairs`,
  `test_run_job`, `test_job_pipeline`, `test_worker_events`, `test_worker_event_evidence`,
  `test_no_change_outcomes`, `test_tool_launcher`, `test_tool_probe`, `test_model_settings`).
- Test: `tests/test_discovery_launcher.py`

**Interfaces:**
- Consumes: Task 3 (`discover`, `ServerProfile`, `HttpFetch`, `FakeOmlx`).
- Produces:
  - `run_preflight(settings_path: Path, requested_model: Optional[str], context_cap: Optional[int], fetch: HttpFetch) -> Dict` returning `{"endpoint", "profile": ServerProfile.as_report(), "context_window", "model_fallback": False, "api_key": "present (value withheld)"}`
  - `config_overrides(profile: ServerProfile, context_window: int, catalog_path: Optional[Path] = None) -> List[str]`
  - `missing_config_pins(argv: Sequence[str], profile: ServerProfile, context_window: int, catalog: Optional[Tuple[Path, str]] = None) -> List[str]` (`catalog` = generated file and its sha256, Task 7)
  - `worker_argv(codex_bin, worktree, profile, context_window, last_message, tool_overrides=(), shell_excludes=(), catalog_path=None) -> List[str]`
  - `main(argv: Optional[Sequence[str]] = None, *, omlx_fetch: HttpFetch = omlx_discovery.http_fetch) -> int`
  - `tests/support.py::run_clean_job(root, repo, *, omlx=None, fake_run=None, worker_edit=None, **contract_overrides) -> Tuple[Dict, FakeRun]` — later test classes wrap it as `self.run_clean_job(...)`
  - `JobOptions` fields `requested_model: Optional[str] = None`, `context_cap: Optional[int] = None`, `omlx_fetch: HttpFetch = omlx_discovery.http_fetch` (replace `list_models`, `canonical_model`, `api_model`, `context_window`)
  - report keys `model_profile` (dict), `context_window` (int), `endpoint` (from profile)
  - CLI: `preflight [--settings] [--model NAME] [--context-cap N]`, `run ... [--model NAME] [--context-cap N] [--load]`
  - `COMPACT_FRACTION = 0.8`; pin `model_auto_compact_token_limit = int(context_window * COMPACT_FRACTION)`

- [ ] **Step 1: Write the failing tests** (`tests/test_discovery_launcher.py`)

```python
def test_preflight_reports_the_discovered_model(self):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = lw.main(["preflight", "--settings", str(self.settings)], omlx_fetch=sp.FakeOmlx("one"))
    receipt = json.loads(out.getvalue())
    self.assertEqual(code, 0)
    self.assertEqual(receipt["profile"]["model"]["api_model"], "Qwen3.5-9B-OptiQ-4bit")
    self.assertEqual(receipt["context_window"], 262144)

def test_context_cap_only_lowers(self):
    receipt = lw.run_preflight(self.settings, None, 32768, sp.FakeOmlx("one"))
    self.assertEqual(receipt["context_window"], 32768)
    receipt = lw.run_preflight(self.settings, None, 10**6, sp.FakeOmlx("one"))
    self.assertEqual(receipt["context_window"], 262144)

def test_worker_argv_pins_the_profile_and_passes_its_self_check(self):
    profile = omlx_discovery.discover(self.settings, fetch=sp.FakeOmlx("one"))
    argv = lw.worker_argv("codex", Path("/wt"), profile, 32768, Path("/r/l.txt"))
    pins = lw.config_overrides_map(argv)
    self.assertEqual(pins["model"], '"Qwen3.5-9B-OptiQ-4bit"')
    self.assertEqual(pins["model_context_window"], "32768")
    self.assertEqual(pins["model_auto_compact_token_limit"], str(int(32768 * 0.8)))
    self.assertEqual(lw.missing_config_pins(argv, profile, 32768), [])
    self.assertTrue(lw.missing_config_pins(argv, profile, 65536))

def test_discovery_error_maps_to_preflight_failed_with_code(self):
    report = self.run_job(omlx=sp.FakeOmlx("two_loaded"))
    self.assertEqual(report["status"], "preflight_failed")
    self.assertEqual(report["discovery_error"]["code"], "model_ambiguous")

def test_no_model_constants_remain(self):
    source = Path(lw.__file__).read_text()
    for token in ("Qwen", "qwen-3.8", "65536", "CANONICAL_MODEL", "EXPECTED_ALIAS"):
        self.assertNotIn(token, source)
```

- [ ] **Step 2:** `python3 -m unittest tests.test_discovery_launcher -v` — FAIL.
- [ ] **Step 3: Implement** the signatures above. `validate_endpoint` accepts
  `http://{127.0.0.1|localhost|[::1]}:<port>/v1`; every `base_url` pin compares to the profile's
  endpoint. Discovery errors become `PreflightError` carrying `code`; the report records
  `discovery_error: {"code", "message", "candidates"}`. `--load` calls `load_model` for
  `--model` before discovery. Delete `tests/test_model_settings.py` (superseded) and port any
  still-relevant assertion (validation of `--context-cap`: integer 4096–1048576) into the new test.
- [ ] **Step 4:** `python3 -m unittest discover -s tests` — PASS.
- [ ] **Step 5:** Commit `feat: route every job through live oMLX discovery`.

### Task 5: Per-attempt recheck

**Files:**
- Modify: `src/local_worker.py` (`_execute` attempt loop)
- Test: `tests/test_discovery_launcher.py`

**Interfaces:**
- Consumes: `omlx_discovery.recheck`.
- Produces: attempt record key `skipped_reason` with value `"model_changed"` or
  `"server_unreachable"`; report `failure_reason` naming old and new model.

- [ ] **Step 1: Write the failing test**

```python
def test_model_swapped_before_attempt_blocks_attempt(self):
    fake = sp.FakeOmlx("one")
    fake.after_calls(3, variant="two_loaded")   # preflight done, swap before attempt 1
    run = sp.FakeRun(worker_edit=self.fix)
    report = self.run_job(omlx=fake, fake_run=run)
    self.assertEqual(run.exec_calls, 0)
    self.assertEqual(report["attempts"][0]["skipped_reason"], "model_changed")
    self.assertEqual(report["status"], "blocked")
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Call `recheck(profile, settings_path, fetch)` immediately before each attempt;
  on a non-`None` result append the skipped attempt and stop the loop (`BlockedError` semantics).
  Add `FakeOmlx.after_calls(n: int, variant: str)` to `tests/support.py`.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: recheck the pinned model before every attempt`.

### Task 6: Spike — Codex catalog entry for a local model (decision gate)

**Files:**
- Create: `scripts/spike_local_catalog.py`, `src/codex_local_model_template.json`,
  `docs/qualification/2026-10-24-catalog-spike.md`

**Interfaces:**
- Produces: `src/codex_local_model_template.json` — a catalog entry with every field Codex 0.164
  requires, values chosen for a local model, and placeholders `{{slug}}`, `{{display_name}}`,
  `{{context_window}}`, `{{input_modalities}}` filled by Task 7.

- [ ] **Step 1:** In a throwaway `CODEX_HOME`, render `codex debug prompt-input` for the local
  provider (the command form recorded in §5) with no catalog and with candidate catalog entries.
  Vary `tool_mode`, `apply_patch_tool_type`, `shell_type`, `base_instructions` (empty vs. the
  fallback's), `truncation_policy`, `input_modalities`.
- [ ] **Step 2:** Pick the entry whose tool list contains a patch tool and a shell tool and no
  multi-agent/code-mode tools, and whose base instructions are not cloud-specific. Confirm with a
  real `codex exec --json` on the pilot repo (`scripts/prepare_pilot.py`): no `Model metadata ...
  not found` error item, no `unsupported call: apply_patch` on stderr, the patch applied with the
  patch tool.
- [ ] **Step 3:** Write the template and the spike record (commands, tool lists per variant, the
  pilot result, and Codex version). If no variant passes Step 2, record that, keep the template
  with the best variant, and Task 7 ships with `catalog: "fallback"` reported.
- [ ] **Step 4:** Commit `docs: record the local-model catalog spike`.

### Task 7: Generated catalog, isolated Codex home, ephemeral sessions

**Files:**
- Create: `src/codex_catalog.py`
- Modify: `src/local_worker.py` (`worker_env`, `_execute`, `worker_argv`), `src/worker_tool_probe.py`
  (`probe_tools` takes `codex_home: Path`)
- Test: `tests/test_codex_catalog.py`, `tests/test_worker_isolation.py`

**Interfaces:**
- Consumes: Task 6 template; Task 4 `worker_argv(..., catalog_path=)`.
- Produces:
  - `codex_catalog.build_local_entry(model: ModelProfile, context_window: int) -> Dict`
  - `codex_catalog.write_catalog(path: Path, entry: Dict) -> str` (returns sha256 hex of the bytes written)
  - `local_worker.job_codex_home(report_dir: Path) -> Path` (`<report_dir>/codex-home`, created empty)
  - worker argv contains `--ephemeral` and `-c model_catalog_json="<path>"`; report keys
    `codex_home`, `catalog_sha256`
  - `local_worker.MIN_CODEX_VERSION = (0, 164, 0)`; `codex_compat(codex_bin: str, run: Callable = subprocess.run) -> Dict` → `{"ok", "version", "missing_flags", "reason"}` (checks `codex --version` and that `codex exec --help` lists `--ephemeral`, `--output-schema`, `--ignore-user-config`, `--ignore-rules`, `--json`); preflight and `run` refuse with `preflight_failed` when not ok
  - `tests/support.py::fake_help(version: str, missing: Sequence[str] = ())` (fake `subprocess.run` for the two help calls)

- [ ] **Step 1: Write the failing tests**

```python
def test_entry_fills_the_template_from_the_profile(self):
    entry = codex_catalog.build_local_entry(self.profile.model, 32768)
    self.assertEqual(entry["slug"], "Qwen3.5-9B-OptiQ-4bit")
    self.assertEqual(entry["context_window"], 32768)
    self.assertIn("image", entry["input_modalities"])

def test_worker_never_sees_the_users_codex_home(self):
    user_home = sp.tmpdir(); (user_home / "AGENTS.md").write_text("route to launcher\n")
    with mock.patch.dict(os.environ, {"CODEX_HOME": str(user_home)}):
        report, run = self.run_clean_job()
    worker = run.calls_of("exec")[0]
    self.assertNotEqual(worker["env"]["CODEX_HOME"], str(user_home))
    self.assertTrue(worker["env"]["CODEX_HOME"].startswith(report["report_dir"]))
    self.assertIn("--ephemeral", worker["argv"])
    self.assertEqual(lw.config_overrides_map(worker["argv"])["model_catalog_json"],
                     json.dumps(str(Path(report["report_dir"]) / "codex-home" / "local-model-catalog.json")))

def test_catalog_drift_is_refused(self):
    argv = self.argv_with_catalog()
    Path(self.catalog).write_text("{}")
    self.assertTrue(lw.missing_config_pins(argv, self.profile, 32768, (self.catalog, self.catalog_sha)))

def test_codex_compatibility_is_checked_at_preflight(self):
    compat = lw.codex_compat("codex", run=sp.fake_help(version="codex-cli 0.163.0"))
    self.assertFalse(compat["ok"]); self.assertIn("0.164.0", compat["reason"])
    self.assertTrue(lw.codex_compat("codex", run=sp.fake_help(version="codex-cli 0.164.0"))["ok"])
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** `missing_config_pins(..., catalog=(path, sha256))` re-hashes the
  file and compares it with the hash `write_catalog` returned. The tool probe uses the same job
  home.
- [ ] **Step 4:** Full suite — PASS.
- [ ] **Step 5:** Commit `feat: isolate the worker home and give Codex local model metadata`.

---

## Phase C — Permissions and robustness

### Task 8: Permissions list and Seatbelt deny profile

**Files:**
- Create: `src/permissions.py`
- Modify: `src/local_worker.py` (`build_parser`/`main`: `permissions show|deny-read PATH|allow-read PATH`)
- Test: `tests/test_permissions.py`

**Interfaces:**
- Produces (in `permissions`):
  - `PERMISSIONS_FILE = "permissions.json"`, `SCHEMA_VERSION = 1`, `SANDBOX_EXEC = "/usr/bin/sandbox-exec"`
  - `DEFAULT_DENY_READ = ("~/.codex", "~/.ssh", "~/.omlx/settings.json", "~/.aws", "~/.config/gh", "~/.config/gcloud", "~/.kube", "~/.docker/config.json", "~/.netrc", "~/.npmrc", "~/.pypirc", "~/.gnupg", "~/Library/Application Support/Google/Chrome", "~/Library/Application Support/Firefox", "~/Library/Safari", "~/Library/Group Containers/2BUA8C4S2C.com.1password")`
  - `@dataclass Permissions(mode: str, deny_read: List[str], protect_primary_checkout: bool, writable_roots: List[str], allowed_domains: List[str])`
  - `class PermissionsError(Exception)`
  - `load_permissions(state_dir: Path) -> Permissions` (creates the default file when missing; refuses unknown keys, `mode != "full_host"` with "sandboxed mode is reserved")
  - `save_permissions(state_dir: Path, perms: Permissions) -> Path` (atomic)
  - `check_conflicts(perms: Permissions, *, repo: Path, worktree_root: Path, state_dir: Path, codex_bin: Path) -> List[str]`
  - `seatbelt_profile(perms: Permissions, *, primary_repo: Path) -> str`
  - `wrapper_available() -> bool`; `wrap_argv(argv: Sequence[str], profile_path: Path) -> List[str]`
  - `find_denials(text: str, perms: Permissions, primary_repo: Path) -> List[Dict[str, str]]` (`{"path", "kind": "read"|"write"}`)

- [ ] **Step 1: Write the failing tests**

```python
def test_default_file_is_created_with_full_host_and_deny_list(self):
    perms = permissions.load_permissions(self.state)
    self.assertEqual(perms.mode, "full_host")
    self.assertIn("~/.ssh", perms.deny_read)
    self.assertTrue((self.state / "permissions.json").is_file())

def test_profile_allows_by_default_and_lists_only_denials(self):
    text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
    self.assertTrue(text.startswith("(version 1)\n(allow default)"))
    self.assertIn(f'(subpath "{os.path.realpath(os.path.expanduser("~/.ssh"))}")', text)
    for literal in ("HEAD", "index", "packed-refs", "config"):
        self.assertIn(f'(literal "{os.path.realpath(self.repo)}/.git/{literal}")', text)
    self.assertLess(text.index(f'(deny file-write* (subpath "{os.path.realpath(self.repo)}"))'),
                    text.index(f'(allow file-write* (subpath "{os.path.realpath(self.repo)}/.git"))'))

def test_profile_escapes_awkward_paths(self):
    repo = sp.make_repo(sp.tmpdir_under(self.root / 'with space "quote" ü'))
    text = permissions.seatbelt_profile(self.perms, primary_repo=repo)
    self.assertIn('\\"quote\\"', text)

def test_conflicting_deny_entry_is_refused(self):
    self.perms.deny_read.append(str(self.repo.parent))
    problems = permissions.check_conflicts(self.perms, repo=self.repo, worktree_root=self.state / "worktrees",
                                           state_dir=self.state, codex_bin=Path("/opt/homebrew/bin/codex"))
    self.assertTrue(any(str(self.repo.parent) in problem for problem in problems))

@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
def test_wrapper_hides_a_denied_path_and_protects_the_primary_checkout(self):
    secret = self.root / "secret.txt"; secret.write_text("s")
    perms = dataclasses.replace(self.perms, deny_read=[str(secret)])
    profile = self.root / "p.sb"; profile.write_text(permissions.seatbelt_profile(perms, primary_repo=self.repo))
    read = subprocess.run(permissions.wrap_argv(["/bin/cat", str(secret)], profile), capture_output=True)
    write = subprocess.run(permissions.wrap_argv(["/usr/bin/touch", str(self.repo / "x")], profile), capture_output=True)
    if read.returncode == 71: self.skipTest("already inside a sandbox")
    self.assertNotEqual(read.returncode, 0)
    self.assertNotEqual(write.returncode, 0)
    self.assertFalse((self.repo / "x").exists())
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Paths are `expanduser` + `realpath`; SBPL strings escape `\` and `"`.
  Profile order: `(allow default)`, `(deny file-read* …deny_read…)`, then when
  `protect_primary_checkout`: `(deny file-write* (subpath R))`, `(allow file-write* (subpath R/.git))`,
  `(deny file-write* (literal R/.git/HEAD) (literal R/.git/index) (literal R/.git/packed-refs) (literal R/.git/config) (subpath R/.git/refs/heads))`.
  `find_denials` matches `Operation not permitted` lines containing a denied path or a path under R.
  CLI subcommands print the file and log each change to `<state>/permissions.log` (the ledger of
  Task 20 also reads it).
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: add the user-owned permissions list and Seatbelt deny profile`.

### Task 9: Wrap worker and launcher steps; informational primary check

**Files:**
- Modify: `src/local_worker.py` (`_execute`, `run_verification`, `native_gate`, `decide_status`,
  `describe_failure`, `new_report`)
- Test: `tests/test_permission_wrapper.py`

**Interfaces:**
- Consumes: Task 8.
- Produces: report keys `permissions: {"mode", "deny_list_applied": bool, "profile_sha256",
  "denials": [...]}`, `primary_changed_during_job: bool`; status `ok` is no longer blocked by a
  primary change when `deny_list_applied` is true.

- [ ] **Step 1: Write the failing tests**

```python
def test_worker_and_verification_run_inside_the_wrapper(self):
    report, run = self.run_clean_job()
    worker = run.calls_of("exec")[0]
    self.assertTrue(worker["wrapped"])
    self.assertEqual(worker["raw_argv"][:2], [permissions.SANDBOX_EXEC, "-f"])
    self.assertTrue(all(call["wrapped"] for call in run.verification_calls()))
    self.assertTrue(report["permissions"]["deny_list_applied"])

def test_parent_commit_during_job_is_reported_not_failed(self):
    def edit(cwd, attempt):
        self.fix(cwd); sp.git(self.repo, "commit", "--allow-empty", "-qm", "parent work")
    report, _ = self.run_clean_job(worker_edit=edit)
    self.assertEqual(report["status"], "ok")
    self.assertTrue(report["primary_changed_during_job"])

def test_without_sandbox_exec_jobs_run_and_say_so(self):
    with mock.patch.object(permissions, "wrapper_available", return_value=False):
        report, run = self.run_clean_job()
    self.assertFalse(report["permissions"]["deny_list_applied"])
    self.assertFalse(run.calls_of("exec")[0]["wrapped"])
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Write `<report_dir>/permissions.sb` per job; wrap the worker argv
  (after the self-check, which checks the inner Codex argv), every verification command, every
  native step and capture. `tests/support.py::FakeRun.__call__` strips a leading
  `sandbox-exec -f <profile>` and records `wrapped: bool` and `raw_argv`, so `calls_of(kind)` keeps
  working for every earlier test; add `FakeRun.verification_calls() -> List[Dict]`. Collect `find_denials` from worker JSONL
  command outputs and verification output into `permissions.denials`. Without the wrapper the old
  rule applies (primary change ⇒ `audit_violation`).
- [ ] **Step 4:** Live check: run the pilot (`scripts/prepare_pilot.py`, `--state-dir` in a temp
  dir) on this Mac; expected status `ok` and `deny_list_applied: true`. Record the command and
  result in `docs/qualification/2026-10-24-catalog-spike.md` under "Wrapper smoke".
- [ ] **Step 5:** Full suite — PASS. Commit `feat: run workers and checks inside the deny wrapper`.

### Task 10: Signals and orphan reaping

**Files:**
- Modify: `src/local_worker.py` (`main` installs handlers; `run_job` writes/removes
  `<state>/active/<job_id>.json`), `src/worker_observability.py` (`run_streaming` reports
  `worker_pgid` to the observer via `process_started`)
- Test: `tests/test_signals.py`

**Interfaces:**
- Produces: `class Interrupted(LauncherError)` with `status = "interrupted"`;
  `EXIT_CODES["interrupted"] = 11`; `reap_orphans(state_dir: Path) -> List[Dict]` (returns killed
  records); active record `{"job_id", "launcher_pid", "worker_pgid", "worker_command", "started_at"}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sigterm_kills_the_worker_group_and_writes_an_interrupted_report(self):
    proc = subprocess.Popen([sys.executable, str(self.launcher), "run", "--task-file", str(self.task),
                             "--state-dir", str(self.state), "--codex-bin", str(self.sleepy_codex),
                             "--settings", str(self.settings)], env=self.env)
    pgid = self.wait_for_active_record()["worker_pgid"]
    proc.send_signal(signal.SIGTERM); proc.wait(30)
    self.assertEqual(proc.returncode, 11)
    self.assertFalse(lw._group_alive(pgid))
    self.assertEqual(self.only_report()["status"], "interrupted")

def test_orphaned_group_of_a_dead_launcher_is_reaped(self):
    orphan = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
    self.write_active(launcher_pid=self.dead_pid(), worker_pgid=orphan.pid, worker_command="/bin/sleep")
    killed = lw.reap_orphans(self.state)
    self.assertEqual([k["worker_pgid"] for k in killed], [orphan.pid])
    self.assertIsNotNone(orphan.wait(10))
```

  (`sleepy_codex` is a stub script that prints one `thread.started` line and sleeps; the
  `omlx` HTTP calls are served by a stub HTTP server on a random loopback port written into the
  test `settings.json`.)

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Handlers for `SIGTERM`, `SIGHUP`, `SIGINT` raise `Interrupted`;
  `run_streaming`'s existing `except BaseException: stop()` kills the group. `reap_orphans` runs
  inside the lease before admission: kill only when the launcher pid is dead, the group is alive,
  and `ps -o command= -p <pgid>` starts with the recorded `worker_command`; log each kill.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `fix: stop orphaned workers when the launcher is interrupted`.

### Task 11: Stall watchdog replaces the fixed attempt cap

**Files:**
- Modify: `src/worker_observability.py` (`run_streaming(..., stall_seconds=None, on_stall=None)`),
  `src/local_worker.py` (`_execute`, `JobOptions.attempt_timeout_cap` removed), `src/run_policy.py`
  (delete `ATTEMPT_SECONDS`)
- Test: `tests/test_stall_watchdog.py`

**Interfaces:**
- Produces:
  - `run_streaming(argv, *, cwd, env, timeout, stdin_data=None, observer=None, terminate_group=None, capture_limit_bytes=None, stall_seconds: Optional[float] = None, on_stall: Optional[Callable[[], bool]] = None) -> Dict` — result gains `"stalled": bool`
  - `local_worker.stall_limit(profile: ServerProfile, context_window: int) -> Dict` → `{"seconds": float, "basis": "measured"|"unknown_rates", "inputs": {...}}`
  - constants `STALL_FLOOR_SECONDS = 120.0`, `STALL_UNKNOWN_RATES_SECONDS = 900.0`, `STALL_SAFETY = 1.5`
  - report `attempts[].limits = {"stall": stall_limit(...), "budget_seconds": ...}`

- [ ] **Step 1: Write the failing tests**

```python
def test_quiet_worker_is_stopped_as_stall(self):
    result = obs.run_streaming(["/bin/sh", "-c", "echo '{}'; sleep 30"], cwd=None, env=os.environ.copy(),
                               timeout=60, stdin_data=b"", stall_seconds=1.0)
    self.assertTrue(result["stalled"]); self.assertFalse(result["timed_out"])

def test_stall_extends_while_server_busy(self):
    calls = []
    result = obs.run_streaming(["/bin/sh", "-c", "sleep 1.5; echo '{}'"], cwd=None, env=os.environ.copy(),
                               timeout=60, stdin_data=b"", stall_seconds=1.0,
                               on_stall=lambda: calls.append(1) or len(calls) == 1)
    self.assertFalse(result["stalled"]); self.assertEqual(result["returncode"], 0)

def test_limit_uses_measured_rates_or_a_conservative_default(self):
    measured = lw.stall_limit(self.profile_with(prefill_tps=500.0, generation_tps=40.0), 32768)
    self.assertEqual(measured["basis"], "measured")
    self.assertAlmostEqual(measured["seconds"], max(120.0, 1.5 * (32768 * 0.8 / 500.0 + 8192 / 40.0)))
    self.assertEqual(lw.stall_limit(self.profile_with(None, None), 32768)["seconds"], 900.0)

def test_no_fixed_attempt_cap_remains(self):
    self.assertFalse(hasattr(policy, "ATTEMPT_SECONDS"))
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Generation term uses `min(max_output_tokens or 8192, 8192)`.
  `on_stall` in `_execute` re-reads `/api/status`; returns `True` (extend once per stall window)
  when `active_requests` or `waiting_requests` > 0. A stalled attempt is recorded with
  `stalled: true` and counts as an environment failure (`stall`) in Task 15.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: replace the fixed attempt cap with a stall watchdog`.

### Task 12: One event parser, one log copy, fast redaction

**Files:**
- Modify: `src/worker_observability.py` (`_Redactor`, `RunObserver`, new `EventAccumulator`),
  `src/local_worker.py` (`parse_worker_log` becomes a wrapper; stop writing
  `attempt-N.jsonl`/`attempt-N.stderr.txt`, reference the observer's files)
- Test: `tests/test_worker_events.py`, `tests/test_worker_observability.py`

**Interfaces:**
- Produces: `EventAccumulator.add(event: Dict) -> None`, `.result() -> Dict` (keys of today's
  `parse_worker_log` result, plus `file_changes: int`); `RunObserver.evidence() -> Dict`;
  `TOOLS` includes `"file_change"`; `progress.json` always carries `phase` and `last_event_at`
  (shown by `status`); logs per attempt are exactly `worker-attempt-N.jsonl`,
  `worker-attempt-N.stderr.log`, `attempt-N.prompt.txt`, `attempt-N.last-message.txt`.

- [ ] **Step 1: Write the failing tests**

```python
def test_live_and_post_run_evidence_are_identical(self):
    lines = sp.load_fixture_text("codex/exec_pilot.jsonl")
    observer = obs.RunObserver(self.dir, secrets=[sp.SECRET])
    for chunk in (lines[i:i + 97].encode() for i in range(0, len(lines), 97)):
        observer("stdout", chunk)
    observer.finish()
    self.assertEqual(observer.evidence(), lw.parse_worker_log(lines))

def test_redaction_is_linear(self):
    redactor = obs._Redactor([sp.SECRET, "another-secret-value"])
    payload = (b"x" * 1024 + sp.SECRET.encode()) * 8192          # ~8.4 MB
    started = time.monotonic()
    out = "".join(redactor.feed(payload[i:i + 65536]) for i in range(0, len(payload), 65536)) + redactor.feed(b"", final=True)
    self.assertLess(time.monotonic() - started, 2.0)
    self.assertNotIn(sp.SECRET, out); self.assertEqual(out.count("[REDACTED]"), 8192)

def test_one_copy_of_each_stream(self):
    report, _ = self.run_clean_job()
    names = sorted(p.name for p in Path(report["report_dir"]).glob("*attempt-1*"))
    self.assertEqual(names, ["attempt-1.last-message.txt", "attempt-1.prompt.txt",
                             "worker-attempt-1.jsonl", "worker-attempt-1.stderr.log"])
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Redactor: per chunk, `text = pending + decoded`; replace every
  secret with `str.replace` (longest first) on all but the last `max_len - 1` characters, keep
  that tail as `pending` unless `final`. Progress writes at most every 0.5 s plus on status change
  and finish.
- [ ] **Step 4:** Run — PASS; full suite PASS (update tests that read `attempt-N.jsonl`).
- [ ] **Step 5:** Commit `refactor: parse and store the worker stream once`.

---

## Phase D — Contract v2, read jobs, classification, escalation

### Task 13: Contract v2

**Files:**
- Create: `src/contract.py`
- Modify: `src/local_worker.py` (`Task` becomes `contract.Contract`; `worker_env` adds
  `environment.pass`; `MAX_TIMEOUT_SECONDS = 14400`)
- Test: `tests/test_contract_v2.py`

**Interfaces:**
- Produces (in `contract`):
  - `KINDS = ("read", "write", "capture")`
  - `@dataclass(frozen=True) Tier(kind: str, model: Optional[str] = None, reasoning_effort: Optional[str] = None)` (`kind` in `("local", "cloud")`)
  - `@dataclass Contract` with fields `task_id, kind, repo_path, base_ref, goal, timeout_seconds, workstream, environment_pass: List[str], tier: Tier, acceptance_criteria, allowed_paths, test_commands, max_attempts, native_plan: Optional[Path], ui_review: bool, question: Optional[str], scope_paths: List[str], answer_schema: Optional[Dict], max_answer_bytes: int, capture: Optional[Dict]`; `Contract.from_payload(payload: Any) -> Contract`; `to_dict()`
  - `ROUTING_ENV_PREFIXES = ("CODEX", "LOCAL_WORKER", "LOCAL_OMLX", "OMLX", "OPENAI", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")`
  - `local_worker.Task = contract.Contract` (compatibility alias)

- [ ] **Step 1: Write the failing tests**

```python
def test_v1_contract_upgrades_to_write(self):
    c = contract.Contract.from_payload(sp.task_payload(self.repo))
    self.assertEqual((c.kind, c.workstream, c.tier.kind), ("write", "task-1", "local"))

def test_read_contract_requires_question_and_schema(self):
    c = contract.Contract.from_payload(self.read_payload())
    self.assertEqual(c.kind, "read"); self.assertEqual(c.allowed_paths, [])
    with self.assertRaises(lw.ContractError):
        contract.Contract.from_payload({**self.read_payload(), "answer_schema": {"type": "object", "patternProperties": {}}})

def test_environment_pass_refuses_routing_names_and_redacts_values(self):
    with self.assertRaises(lw.ContractError):
        contract.Contract.from_payload({**self.write_payload(), "environment": {"pass": ["OPENAI_API_KEY"]}})
    with mock.patch.dict(os.environ, {"JAVA_HOME": "/opt/jdk-secretish"}):
        report, run = self.run_clean_job(environment={"pass": ["JAVA_HOME"]})
    self.assertEqual(run.verification_calls()[0]["env"]["JAVA_HOME"], "/opt/jdk-secretish")
    self.assertNotIn("/opt/jdk-secretish", Path(report["report_dir"], "report.json").read_text())

def test_cloud_tier_needs_a_model(self):
    with self.assertRaises(lw.ContractError):
        contract.Contract.from_payload({**self.write_payload(), "tier": {"cloud": ""}})
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Unknown keys refused per kind; `answer_schema` may use only
  `read_answers.SUPPORTED_SCHEMA_KEYWORDS` (Task 14 defines it; this task imports it). Passed
  values ≥ 4 characters join the job's redaction secrets.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: add contract v2 with read, write and capture jobs`.

### Task 14: Read jobs with verified citations

**Files:**
- Create: `src/read_answers.py`
- Modify: `src/local_worker.py` (`_execute` branches on `kind == "read"`: worktree at `base_ref`,
  `-s read-only`, `--output-schema <report_dir>/answer.schema.json`, no verification, no patch)
- Test: `tests/test_read_answers.py`, `tests/test_read_jobs.py`

**Interfaces:**
- Produces:
  - `SUPPORTED_SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "minItems", "maxItems", "maxLength", "minimum", "maximum", "description"})`
  - `envelope_schema(answer_schema: Dict) -> Dict` (object with required `answer` and `citations[]` of `{path, line_start, line_end, quote}`)
  - `validate(instance: Any, schema: Dict) -> List[str]` (JSON-pointer-prefixed messages)
  - `verify_citations(worktree: Path, citations: List[Dict]) -> Dict` → `{"total", "verified", "failures": [{"index", "reason"}]}`
  - report keys for read jobs: `answer` (validated `answer` only), `citations`, `status` in `("ok", "answer_invalid")`; `EXIT_CODES["answer_invalid"] = 12`

- [ ] **Step 1: Write the failing tests**

```python
def test_citation_must_quote_the_cited_lines(self):
    (self.wt / "a.swift").write_text("let x = 1\r\n\tlet  y = 2\n")
    result = read_answers.verify_citations(self.wt, [
        {"path": "a.swift", "line_start": 2, "line_end": 2, "quote": "let y = 2"},
        {"path": "a.swift", "line_start": 9, "line_end": 9, "quote": "nope"},
        {"path": "../etc/passwd", "line_start": 1, "line_end": 1, "quote": "root"}])
    self.assertEqual((result["total"], result["verified"]), (3, 1))

def test_schema_subset_validation(self):
    schema = {"type": "object", "required": ["controls"], "additionalProperties": False,
              "properties": {"controls": {"type": "array", "items": {"type": "string"}, "maxItems": 2}}}
    self.assertEqual(read_answers.validate({"controls": ["a"]}, schema), [])
    self.assertTrue(read_answers.validate({"controls": ["a", "b", "c"], "x": 1}, schema))

def test_read_job_returns_only_a_verified_answer(self):
    report, run = self.run_read_job(last_message=self.answer_with(bad_citation=True))
    self.assertEqual(report["status"], "answer_invalid")
    self.assertNotIn("answer", report)
    worker = run.calls_of("exec")[0]["argv"]
    self.assertEqual(worker[worker.index("-s") + 1], "read-only")
    self.assertIn("--output-schema", worker)
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Whitespace normalisation: collapse runs of spaces/tabs, strip `\r`,
  compare per line inside the cited range. Answer bytes above `max_answer_bytes` ⇒
  `answer_invalid`. The self-check for read jobs expects `-s read-only` and
  `sandbox_mode="read-only"` instead of full access.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: add read jobs with schema-checked, citation-verified answers`.

### Task 15: Failure classification and next action

**Files:**
- Create: `src/classify.py`
- Modify: `src/local_worker.py` (`run_job` attaches `classification`; JSON output keys of spec §11)
- Test: `tests/test_classify.py`

**Interfaces:**
- Produces: `local_worker` performs one automatic in-job retry when `classify` returns
  `retry_local` with an `auto_fix` (`{"context_cap": int}` or `{"wait_for_server": seconds}`) and
  the budget allows; report key `auto_retries: [{"code", …fix}]`. `FakeRun(first_stream=…)` makes
  the first worker call end with that error text (`tests/support.py`).
- Produces (in `classify`):
  - `ENVIRONMENT_CODES = ("server_unreachable", "server_loading", "model_not_loaded", "model_ambiguous", "model_changed", "context_overflow", "server_memory", "tool_unavailable", "secret_read_denied", "missing_env", "missing_tool", "simulator_unavailable", "stall", "budget_exhausted", "policy_refused", "verification_blocked", "deny_list_unavailable", "interrupted")`
  - `CAPABILITY_CODES = ("tests_failed", "out_of_scope", "no_change", "invalid_answer", "unverified_answer")`
  - `NEXT_ACTIONS = ("done", "retry_local", "escalate_cloud", "needs_user")`
  - `@dataclass Classification(cls: str, code: str, evidence: List[str], remediation: str, next_action: str, plugin_suggestion: Optional[str], auto_fix: Optional[Dict] = None)` with `as_dict()` (key `class` for `cls`); a successful job is `cls="none", code="ok", next_action="done"`
  - `classify(report: Dict) -> Classification`
  - `--json` output top-level keys exactly: `job_id, kind, tier, model, status, classification, answer, patch_path, screenshots, tests, tokens, durations, report_path`

- [ ] **Step 1: Write the failing tests** — one table-driven test over synthetic reports:

```python
CASES = [
    ({"status": "ok"}, ("ok", "done")),
    ({"status": "worker_failed", "attempts": [{"stderr_excerpt": "error=unsupported call: apply_patch", "returncode": 1}]},
     ("tool_unavailable", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"terminal_failures": ["turn.failed"], "error_text": "context length exceeded"}]},
     ("context_overflow", "retry_local")),
    ({"status": "worker_failed", "attempts": [{"stalled": True}]}, ("stall", "retry_local")),
    ({"status": "tests_failed", "attempts": [{}, {}], "max_attempts": 2}, ("tests_failed", "escalate_cloud")),
    ({"status": "audit_violation", "violations": [{"path": "x", "reason": "outside allowed_paths"}]}, ("out_of_scope", "escalate_cloud")),
    ({"status": "worker_failed", "permissions": {"denials": [{"path": "~/.npmrc", "kind": "read"}]}}, ("secret_read_denied", "needs_user")),
    ({"status": "answer_invalid", "citations": {"total": 3, "verified": 1}}, ("unverified_answer", "escalate_cloud")),
    ({"status": "preflight_failed", "discovery_error": {"code": "model_ambiguous"}}, ("model_ambiguous", "needs_user")),
]
def test_cases(self):
    for report, (code, action) in CASES:
        with self.subTest(code=code):
            c = classify.classify(report)
            self.assertEqual((c.code, c.next_action), (code, action))

def test_server_crash_mid_attempt_is_environment(self):
    report = {"status": "tests_failed", "attempts": [{"returncode": 1, "stderr_excerpt": "error sending request: Connection refused (os error 61)"}]}
    c = classify.classify(report)
    self.assertEqual((c.cls, c.code, c.next_action), ("environment", "server_unreachable", "retry_local"))

def test_retry_local_is_offered_once_per_code(self):
    report = dict(CASES[1][0], previous_codes=["tool_unavailable"])
    self.assertEqual(classify.classify(report).next_action, "needs_user")

def test_context_overflow_is_retried_once_with_a_lower_cap(self):
    run = sp.FakeRun(worker_edit=self.fix, first_stream="context length exceeded")
    report, run = sp.run_clean_job(self.root, self.repo, fake_run=run)
    self.assertEqual(report["status"], "ok")
    self.assertEqual(report["auto_retries"], [{"code": "context_overflow", "context_window": int(262144 * 0.75)}])
    self.assertEqual(run.exec_calls, 2)
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Rules read only launcher evidence: discovery errors, attempt
  `stalled`/`timed_out`/`returncode`/`terminal_failures`, typed error item text
  (`context length`/`maximum context`/`prompt is too long` ⇒ `context_overflow`; oMLX `507` or
  `memory` ⇒ `server_memory`; `Connection refused`/`os error 61` ⇒ `server_unreachable`;
  `unsupported call` ⇒ `tool_unavailable`), verification `command not found`/exit 127 ⇒
  `missing_tool`, permissions denials, audit, read-job results. Each environment code has a
  remediation string and, when the plugin itself should change (`tool_unavailable`, `stall`
  with `unknown_rates`, `context_overflow`), a `plugin_suggestion`. `retry_local` only when the
  launcher can apply the fix per job (e.g. lower `--context-cap` to 75 % of the failing size,
  restart after server returns) and the code is not in `previous_codes`.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: classify failures and tell the planner what to do next`.

### Task 16: Budgets per workstream and tier

**Files:**
- Modify: `src/run_policy.py` (rewrite `Workstream`), `src/local_worker.py` (`run_job` claim/record,
  CLI `workstream reset --repo PATH --workstream NAME`)
- Test: `tests/test_run_policy.py` (rewrite)

**Interfaces:**
- Produces:
  - `Workstream(state_dir, repo_identity, name, kind, tier: str = "local")`
  - `claim(job_id, requested_seconds, max_attempts) -> Dict`
  - `record_attempt(job_id, *, capability_failure: bool, has_patch: bool) -> Dict`
  - `finish(job_id, elapsed_seconds) -> None`
  - `reset(state_dir, repo_identity, name, actor: str) -> Dict` (logs to `<state>/workstreams/reset.log`)
  - `class PolicyStop(RuntimeError)` with attribute `next_action: str` (`"escalate_cloud"` for the local breaker, `"needs_user"` for the cloud allowance)

- [ ] **Step 1: Write the failing tests**

```python
def test_environment_failures_never_trip_the_breaker(self):
    w = self.make()
    for job in ("a", "b", "c"):
        w.claim(job, 60, 1); w.record_attempt(job, capability_failure=False, has_patch=False); w.finish(job, 1)
    w.claim("d", 60, 1)

def test_local_breaker_escalates_instead_of_blocking(self):
    w = self.make()
    for job in ("a", "b"):
        w.claim(job, 60, 1); w.record_attempt(job, capability_failure=True, has_patch=False); w.finish(job, 1)
    with self.assertRaises(policy.PolicyStop) as ctx:
        w.claim("c", 60, 1)
    self.assertEqual(ctx.exception.next_action, "escalate_cloud")
    self.make(tier="cloud").claim("c-cloud", 60, 1)

def test_renaming_cannot_reset_but_reset_can(self):
    self.trip(self.make())
    with self.assertRaises(policy.PolicyStop): self.make().claim("renamed", 60, 1)
    policy.reset(self.root, "repo", "feature", actor="user")
    self.make().claim("after-reset", 60, 1)

def test_default_workstream_is_the_task_id(self):
    self.assertEqual(contract.Contract.from_payload(sp.task_payload(self.repo)).workstream, "task-1")
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** State file key includes the tier; keep the file locking, corruption
  and reservation checks of the current implementation; drop `empty_streak` counting for
  non-capability attempts. A `PolicyStop` in `run_job` sets `classification.next_action` from the
  exception.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `fix: scope budgets to the task and escalate instead of blocking`.

### Task 17: Cloud tier and escalation links

**Files:**
- Modify: `src/codex_catalog.py` (`CloudModel`, `list_cloud_models`), `src/local_worker.py`
  (`models --cloud` subcommand; tier `cloud` argv; `escalates` field), `tests/support.py`
  (`fake_completed(stdout: str = "", returncode: int = 0)`)
- Create: `docs/qualification/2026-10-24-cloud-auth-spike.md`
- Test: `tests/test_cloud_tier.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) CloudModel(slug: str, display_name: str, description: str, context_window: int, reasoning_levels: List[str], default_reasoning: Optional[str])`
  - `list_cloud_models(codex_bin: str, run: Callable = subprocess.run) -> List[CloudModel]` (visibility `list` only, catalog order)
  - CLI `models [--cloud] [--json]` (local profile from discovery; cloud list from Codex)
  - `run --escalates <job_id>` sets report `escalates`; contract `tier: {"cloud": slug, "reasoning_effort": …}`
  - cloud argv: `codex exec --ignore-user-config --ignore-rules --ephemeral --json -s danger-full-access -m <slug> [-c model_reasoning_effort="…"]`, no local provider pins, inside the deny wrapper with `~/.codex` handled per the spike decision

- [ ] **Step 1: Spike (recorded in the doc):** run `codex exec --ephemeral -m <cheapest listed
  model> "reply ok"` once **only if the user allows a cloud call**; otherwise test argv and auth
  file access with `codex login status` under (a) a job home holding a copy of `auth.json`
  (refresh written back after the run) and (b) the real home with `~/.codex/auth.json` readable
  and the rest of `~/.codex` denied. Pick the option that keeps the most of `~/.codex` hidden and
  survives token refresh; record it.
- [ ] **Step 2: Write the failing tests**

```python
def test_cloud_catalog_is_read_live(self):
    run = sp.fake_completed(stdout=sp.load_fixture_text("codex/debug_models.json"))
    models = codex_catalog.list_cloud_models("codex", run=lambda *a, **k: run)
    self.assertIn("gpt-6-luna", [m.slug for m in models])
    self.assertTrue(all(m.description for m in models))

def test_cloud_job_uses_the_same_pipeline_and_links_its_origin(self):
    report, run = self.run_clean_job(tier={"cloud": "gpt-6-luna", "reasoning_effort": "low"}, escalates="J1")
    argv = run.calls_of("exec")[0]["argv"]
    self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
    self.assertNotIn("model_providers.local-omlx.base_url", " ".join(argv))
    self.assertEqual(report["escalates"], "J1")
    self.assertIn("tests", report); self.assertIn("violations", report)

def test_no_cloud_slug_is_hardcoded(self):
    for path in Path(lw.__file__).parent.glob("*.py"):
        self.assertNotRegex(path.read_text(), r"gpt-\d")
```

- [ ] **Step 3:** Run — FAIL. **Step 4: Implement** per the interfaces and the spike decision; the
  self-check for cloud jobs pins `-m <slug>`, `--ephemeral`, `--ignore-user-config`,
  `--ignore-rules`, the sandbox flag and the absence of any `model_providers.*` override.
- [ ] **Step 5:** Run — PASS; full suite PASS. Commit `feat: run cloud escalations through the same pipeline`.

---

## Phase E — Native verification and screenshots

### Task 18: Platform-generic native plan v2; remove legacy confinement

**Files:**
- Modify: `src/native_readiness.py` (`_KEYS_V2`, v1 upgrade, `qualify(..., destination, ...)`,
  `platform_for_udid`), `src/apple_verification.py` (keep only `validate_simulator_udid`,
  `simctl_devices(run) -> Dict[str, Dict]`; delete profile/probe/testmanagerd/pty/mach code and
  `DEVICE_FAMILY`), `src/local_worker.py` (delete `SANDBOX_*`, `WORKSPACE_SANDBOX_BACKEND`,
  `sandbox_argv*`, `_probe_codex_sandbox`, the `apple.BACKEND` branches; rename
  `--apple-simulator-udid` to `--simulator-udid` keeping the old name as an alias)
- Delete: `tests/test_apple_verification.py` parts for the profile, `tests/test_testmanagerd_ipc.py`,
  `tests/test_xctest_startup.py`, `tests/test_pty_permissions.py`
- Modify: `tests/support.py` (`simctl_devices(runtimes: Dict[str, List[Dict]]) -> Dict`)
- Test: `tests/test_native_readiness.py`, `tests/test_native_gate_integration.py`

**Interfaces:**
- Produces:
  - native plan v2 keys: `version` (2), `destination` (`{"simulator": "<UDID>"}` or `{"macos": true}`), `build`, `install` (optional for macOS), `launch`, `test`, `app_bundle`, `result_bundle`, `bundle_id`, optional `screenshots`, `capture_sources`, `capture_overlay_paths`
  - `qualify(plan_path, worktree, report_dir, destination: Dict[str, Any], run, deadline) -> Dict`
  - `platform_for_udid(udid: str, devices: Dict) -> str` (`"iOS"`, `"tvOS"`, `"watchOS"`, `"visionOS"` from the runtime identifier)

- [ ] **Step 1: Write the failing tests**

```python
def test_any_simulator_platform_is_accepted(self):
    for runtime in ("com.apple.CoreSimulator.SimRuntime.iOS-27-0", "com.apple.CoreSimulator.SimRuntime.tvOS-27-0",
                    "com.apple.CoreSimulator.SimRuntime.watchOS-12-0", "com.apple.CoreSimulator.SimRuntime.xrOS-3-0"):
        with self.subTest(runtime=runtime):
            devices = sp.simctl_devices({runtime: [{"udid": sp.APPLE_UDID, "isAvailable": True}]})
            self.assertTrue(native.platform_for_udid(sp.APPLE_UDID, devices))

def test_v1_plan_upgrades_to_a_simulator_destination(self):
    plan = native._load(self.v1_plan_path, self.wt, self.report_dir, sp.APPLE_UDID)
    self.assertEqual(plan["destination"], {"simulator": sp.APPLE_UDID})

def test_macos_destination_skips_install(self):
    evidence = native.qualify(self.macos_plan, self.wt, self.report_dir, {"macos": True}, self.fake_run, self.deadline)
    self.assertEqual([c["stage"] for c in evidence["checks"]], ["build", "launch", "test"])

def test_legacy_confinement_is_gone(self):
    self.assertFalse(hasattr(lw, "sandbox_argv"))
    self.assertFalse(hasattr(lw.apple, "build_profile"))
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** xcodebuild validation keeps its rules but checks the destination
  selector against `destination` (`id=` for simulators, `platform=macOS` for macOS). Simulator
  lease (pool) unchanged.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `refactor: make native verification platform-generic and drop legacy confinement`.

### Task 19: Screenshots — sources, overlay, capture jobs

**Files:**
- Create: `src/screenshots.py`
- Modify: `src/local_worker.py` (`_execute` runs captures for `ui_review` write jobs and `capture`
  jobs; `EXIT_CODES["capture_failed"] = 13`)
- Modify: `tests/support.py` (`write_png(path, width, height, rgb, dot=None) -> Path`, stdlib `zlib`)
- Test: `tests/test_screenshots.py`

**Interfaces:**
- Produces (in `screenshots`):
  - `@dataclass(frozen=True) CaptureSource(name: str, repo_path: Path, ref: str, scheme: Optional[str], destination: Dict[str, Any])`
  - `capture_source(plan: Dict, source: CaptureSource, worktree: Path, out_dir: Path, run: Callable) -> List[Dict]` (`{"name", "path", "method": "simctl"|"xcresult", "uniform": bool, "error": Optional[str]}`)
  - `apply_overlay(result_tree: Path, before_tree: Path, overlay_paths: List[str], allowed_roots: List[str]) -> List[str]`
  - `png_is_uniform(path: Path) -> bool`
  - `write_index(out_dir: Path, results: Dict[str, List[Dict]]) -> Path` (Markdown table: one row per screenshot name, one column per source)
  - report key `screenshots: {"index": path, "sources": {name: [...]}}`

- [ ] **Step 1: Write the failing tests**

```python
def test_uniform_png_is_detected(self):
    black = sp.write_png(self.dir / "black.png", 4, 4, (0, 0, 0))
    mixed = sp.write_png(self.dir / "mixed.png", 4, 4, (0, 0, 0), dot=(255, 255, 255))
    self.assertTrue(screenshots.png_is_uniform(black)); self.assertFalse(screenshots.png_is_uniform(mixed))

def test_overlay_only_copies_declared_harness_paths(self):
    copied = screenshots.apply_overlay(self.after, self.before, ["UITests/Settings.swift", "App/Main.swift"],
                                       allowed_roots=["UITests"])
    self.assertEqual(copied, ["UITests/Settings.swift"])

def test_ui_review_captures_before_after_and_reference(self):
    report, run = self.run_ui_job(references=[{"name": "original", "ref": "main", "scheme": "TiviaryOriginal"}])
    self.assertEqual(sorted(report["screenshots"]["sources"]), ["after", "before", "original"])
    index = Path(report["screenshots"]["index"]).read_text()
    self.assertIn("| settings |", index)

def test_capture_job_with_a_blank_frame_fails_with_reason(self):
    report, _ = self.run_capture_job(frames="black")
    self.assertEqual(report["status"], "capture_failed")
    self.assertIn("uniform", report["failure_reason"])
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** `simctl` method: `xcrun simctl launch --terminate-running-process
  <udid> <bundle> <args…>` or `simctl openurl`, wait `settle_seconds` (default 3), `simctl io
  <udid> screenshot <path>`. `xcresult` method: `xcrun xcresulttool export attachments --path
  <bundle> --output-path <dir>` and pick the named attachment. `png_is_uniform` decodes with
  `zlib` and PNG filter reversal (types 0–4) for 8-bit RGB/RGBA. A source that cannot reach a
  screen yields `error` and the index cell says "no image: <reason>". Captures run inside the
  deny wrapper and the simulator lease.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: capture before, after and reference screenshots`.

---

## Phase F — Observability

### Task 20: Session ledger and summary

**Files:**
- Create: `src/ledger.py`
- Modify: `src/local_worker.py` (`run_job` appends; `summary [--session ID] [--state-dir]` subcommand)
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces (in `ledger`):
  - `session_id(explicit: Optional[str] = None) -> str` (`explicit`, else `CODEX_THREAD_ID`, else `"manual-<UTC date>"`)
  - `append_job(state_dir: Path, session: str, report: Dict) -> Path` (`<state>/sessions/<session>/ledger.jsonl`)
  - `planner_usage(session: str, codex_home: Path) -> Optional[Dict]` (last `token_count.info.total_token_usage` of the rollout whose `session_meta.id` equals `session`; children found via the parent link a real `spawn_agent` rollout carries — inspect one during implementation; unlinked ⇒ `subagents: "not linked"`)
  - `build_summary(state_dir: Path, session: str, codex_home: Path) -> Dict`
  - `render_markdown(summary: Dict) -> str`
  - `write_summary(state_dir: Path, session: str, codex_home: Path) -> Tuple[Path, Path]` (`summary.md`, `summary.json`)

- [ ] **Step 1: Write the failing tests**

```python
def test_summary_totals_local_cloud_and_planner_tokens(self):
    for report in (self.job("J1", tier="local", tokens=(47610, 39169, 627, 190), status="tests_failed", code="tests_failed"),
                   self.job("J2", tier="cloud", model="gpt-6-luna", tokens=(12000, 0, 900, 0), status="ok", escalates="J1")):
        ledger.append_job(self.state, "S1", report)
    codex_home = self.planner_home(rollout="codex/rollout_planner.jsonl", session="S1")
    summary = ledger.build_summary(self.state, "S1", codex_home)
    self.assertEqual(summary["totals"]["local"]["input_tokens"], 47610)
    self.assertEqual(summary["totals"]["cloud"]["output_tokens"], 900)
    self.assertIsNotNone(summary["planner"])
    self.assertEqual(summary["escalations"], [{"from": "J1", "to": "J2", "reason": "tests_failed"}])
    md = ledger.render_markdown(summary)
    self.assertIn("Suggested fixes for this plugin", md); self.assertIn("J2", md)

def test_missing_data_is_shown_as_missing(self):
    ledger.append_job(self.state, "S2", self.job("J3", tokens=None))
    md = ledger.render_markdown(ledger.build_summary(self.state, "S2", self.empty_home()))
    self.assertIn("not reported", md); self.assertNotIn("| 0 |", md)
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Ledger line fields: `job_id, kind, tier, model, profile (model id,
  context_window), attempts, durations {worker, verification, native, screenshots, total},
  tokens {input, cached, output, reasoning}, status, classification, escalates, report_path,
  screenshots_index`. Summary sections: jobs table, totals per tier and per model, local share of
  all tokens (planner included when known), first-try success rate, escalations, screenshots,
  suggested plugin fixes (from classifications and `permissions.log`), planner tokens.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: record every job and render the session summary`.

### Task 21: macOS notifications

**Files:**
- Create: `src/notify.py`
- Modify: `src/local_worker.py` (notify on job end, escalation-worthy classification, `needs_user`,
  plugin suggestion, deny list not applied, summary written)
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `notify(kind: str, title: str, message: str, *, run: Callable = subprocess.run) -> bool`;
  env `LOCAL_WORKER_NOTIFY` (`"0"` disables); argv form
  `["/usr/bin/osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, message]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_message_travels_as_argv_and_is_bounded(self):
    calls = []
    notify.notify("job", 'Job "J1"', "x" * 500 + sp.SECRET, run=lambda argv, **k: calls.append(argv) or sp.fake_completed())
    argv = calls[0]
    self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
    self.assertLessEqual(len(argv[-1]), 200)

def test_disabled_and_failing_notifications_never_raise(self):
    with mock.patch.dict(os.environ, {"LOCAL_WORKER_NOTIFY": "0"}):
        self.assertFalse(notify.notify("job", "t", "m", run=self.fail_if_called))
    self.assertFalse(notify.notify("job", "t", "m", run=lambda *a, **k: (_ for _ in ()).throw(OSError("no"))))

def test_job_notification_carries_no_secret(self):
    report, sent = self.run_clean_job_capturing_notifications()
    self.assertTrue(sent); self.assertNotIn(sp.SECRET, json.dumps(sent))
```

- [ ] **Step 2:** Run — FAIL. **Step 3: Implement**; the caller redacts before calling.
- [ ] **Step 4:** Run — PASS. **Step 5:** Commit `feat: notify the user about finished and blocked jobs`.

---

## Phase G — Archive

### Task 22: Archive fixes

**Files:**
- Modify: `src/worktree_lifecycle.py` (`archive`, `snapshot_commit`, `create_bundle`, new `prune`,
  CLI `archive --resume --skip-ignored --ignored-cap BYTES`, `prune --older-than DAYS [--dry-run]`)
- Test: `tests/test_worktree_lifecycle.py`

**Interfaces:**
- Produces: `archive(repo, state_dir, job_id, *, resume: bool = False, skip_ignored: bool = False, ignored_cap: int = 536870912) -> Dict`;
  `prune(repo, state_dir, older_than_days: int, dry_run: bool) -> List[Dict]`; receipt gains
  `ignored_files: {"count", "bytes", "stored": "copy"|"skipped"}`, `bundle_basis: <base commit>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_existing_ref_is_detected_before_any_work(self):
    sp.git(self.repo, "update-ref", f"refs/local-worker/archive/{self.job}", "HEAD")
    with self.assertRaises(life.Refusal):
        life.archive(self.repo, self.state, self.job)
    self.assertFalse((self.state / "archives" / self.job).exists())

def test_resume_recovers_a_partial_archive_while_the_worktree_exists(self):
    with mock.patch.object(life, "create_bundle", side_effect=life.LifecycleError("disk full")):
        with self.assertRaises(life.LifecycleError): life.archive(self.repo, self.state, self.job)
    receipt = life.archive(self.repo, self.state, self.job, resume=True)
    self.assertEqual(receipt["status"], "archived")

def test_ignored_files_stay_out_of_git_objects_and_bundle_has_a_basis(self):
    (self.wt / "build").mkdir(); (self.wt / "build" / "big.bin").write_bytes(b"\0" * 4096)
    receipt = life.archive(self.repo, self.state, self.job)
    tree = sp.git(self.repo, "ls-tree", "-r", "--name-only", receipt["recovery_commit"]).stdout.decode()
    self.assertNotIn("build/big.bin", tree)
    self.assertTrue((Path(receipt["archive"]) / "build" / "big.bin").is_file())
    self.assertEqual(receipt["bundle_basis"], self.base_commit)

def test_ignored_over_cap_refuses_unless_skipped(self):
    with self.assertRaises(life.Refusal):
        life.archive(self.repo, self.state, self.job, ignored_cap=1024)
    self.assertEqual(life.archive(self.repo, self.state, self.job, ignored_cap=1024, skip_ignored=True)["ignored_files"]["stored"], "skipped")
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Existence check uses the exit status of `show-ref --verify --quiet`.
  Snapshot: `git add --all` (no `--force`); ignored files only in `files/`. Bundle:
  `git bundle create <file> <ref> ^<base_commit>`. Resume is allowed only while the worktree
  still exists and no receipt exists (deletes our partial dir and our ref, then reruns), or with
  a `prepared` receipt whose evidence verifies and whose worktree is gone (marks `archived`).
  `prune` deletes only `archived` receipts older than N days, their dir and ref.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `fix: make archiving resumable and stop bloating the repository`.

---

## Phase H — Plugin, setup, skills

### Task 23: Manifest layout, branding, single version source

**Files:**
- Move: `plugin.json` → `.codex-plugin/plugin.json` (first-party schema: `name`, `version`,
  `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `skills: "./skills/"`,
  `interface`)
- Create: `assets/logo.png`, `assets/logo-dark.png`, `assets/composer-icon.png`, `assets/screenshot-summary.png`,
  `scripts/check_plugin_install.py`
- Modify: `src/plugin_setup.py` (`package_root`, `validate_package`, delete `PLUGIN_VERSION`),
  `tests/test_plugin_package.py`
- Test: `tests/test_plugin_package.py`

**Interfaces:**
- Produces: `plugin_setup.manifest_path(root: Path) -> Path`, `plugin_setup.plugin_version(root: Path) -> str`;
  `interface` keys `displayName, shortDescription, longDescription, developerName, category,
  capabilities, brandColor, brandColorDark, logo, logoDark, composerIcon, screenshots, defaultPrompt`.

- [ ] **Step 1: Write the failing tests**

```python
def test_manifest_uses_the_first_party_layout_and_branding(self):
    manifest = load_json(ROOT / ".codex-plugin" / "plugin.json")
    self.assertFalse((ROOT / "plugin.json").exists())
    for key in ("logo", "composerIcon", "brandColor", "defaultPrompt", "screenshots"):
        self.assertIn(key, manifest["interface"])
        if key in ("logo", "composerIcon"): self.assertTrue((ROOT / manifest["interface"][key]).is_file())

def test_version_has_one_source(self):
    self.assertEqual(plugin_setup.plugin_version(ROOT), load_json(ROOT / ".codex-plugin" / "plugin.json")["version"])
    self.assertNotIn("PLUGIN_VERSION =", (ROOT / "src" / "plugin_setup.py").read_text())
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Icons: simple original geometric mark (no third-party marks), PNG
  written by a small stdlib script or drawn by hand; brand colors chosen to pass 4.5:1 contrast
  on light and dark. `check_plugin_install.py` exports `git archive HEAD` to a temp dir, adds it
  as a marketplace in a temp `CODEX_HOME`, installs it, and asserts `codex debug prompt-input`
  lists `codex-local-worker:local-worker`, `:setup`, `:doctor`.
- [ ] **Step 4:** Run tests — PASS; run `python3 scripts/check_plugin_install.py` — prints `ok`.
- [ ] **Step 5:** Commit `feat: adopt the first-party plugin layout with branding`.

### Task 24: Setup migration keeps the user's completion rule

**Files:**
- Modify: `src/plugin_setup.py` (`managed_block` → `pointer_block`; `apply_block` migration;
  CLI `--apply [--pointer]`)
- Delete: `skills/local-worker/references/global-policy.md`
- Test: `tests/test_plugin_setup.py`, `tests/test_legacy_migration.py`

**Interfaces:**
- Produces:
  - `USER_COMPLETION_RULE: str` — the section of `skills/local-worker/references/global-policy.md`
    at commit `dc88cad` from the line `## Global completion rule: commit, push, clean up worktrees`
    to the end of file (17 lines), byte-for-byte; `USER_COMPLETION_RULE_SHA256 = "ee423c25bfdaeb041b80a703ff434caf7ba210c01a5020794e79e34e33acc516"`
  - `pointer_block(root: Path) -> str` (markers `<!-- local-worker begin -->`/`end`, two lines: skill path and `doctor` command; no policy text)
  - `migrate(text: str, root: Path, pointer: bool) -> Tuple[str, str]` (new text, reason in `("noop", "installed_pointer", "removed_block", "migrated_block", "migrated_legacy", "migrated_legacy_pointer")`)

- [ ] **Step 1: Write the failing tests**

```python
def test_rule_constant_is_the_users_text(self):
    self.assertEqual(hashlib.sha256(plugin_setup.USER_COMPLETION_RULE.encode()).hexdigest(),
                     plugin_setup.USER_COMPLETION_RULE_SHA256)

def test_old_managed_block_becomes_the_users_rule_outside_markers(self):
    before = "# Mine\n\n" + OLD_MANAGED_BLOCK + "\nkeep me\n"
    after, reason = plugin_setup.migrate(before, ROOT, pointer=False)
    self.assertEqual(reason, "migrated_block")
    self.assertIn(plugin_setup.USER_COMPLETION_RULE, after)
    self.assertNotIn("<!-- local-worker begin -->", after)
    self.assertTrue(after.startswith("# Mine\n") and after.endswith("keep me\n"))

def test_rule_is_never_duplicated_and_second_apply_is_noop(self):
    once, _ = plugin_setup.migrate(OLD_MANAGED_BLOCK, ROOT, pointer=True)
    twice, reason = plugin_setup.migrate(once, ROOT, pointer=True)
    self.assertEqual((twice, reason), (once, "noop"))
    self.assertEqual(once.count("## Global completion rule"), 1)

def test_plain_install_adds_nothing_without_pointer(self):
    self.assertEqual(plugin_setup.migrate("# Mine\n", ROOT, pointer=False), ("# Mine\n", "noop"))

def test_legacy_exact_policy_keeps_only_the_rule(self):
    after, reason = plugin_setup.migrate(LEGACY_TEXT, ROOT, pointer=False)
    self.assertEqual(reason, "migrated_legacy")
    self.assertIn("## Global completion rule", after)
    self.assertNotIn("Route all implementation", after)
```

  (`OLD_MANAGED_BLOCK` is `managed_block(root)` as produced at commit `dc88cad`, stored as a test
  fixture string; `LEGACY_TEXT` is `evidence/global-default/AGENTS.md`.)

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** Backups, atomic writes, override (`AGENTS.override.md`) and marker
  safety rules stay as today. When `USER_COMPLETION_RULE` (whitespace-normalised) already appears
  outside markers, it is not added again. `--check` reports `would_change` and `reason` without
  writing.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `feat: keep the user's completion rule outside the plugin`.

### Task 25: Doctor, skills, README

**Files:**
- Modify: `src/local_worker.py` (`doctor` subcommand), `skills/local-worker/SKILL.md`,
  `skills/setup/SKILL.md`, `skills/doctor/SKILL.md`, `skills/local-worker/references/operating-policy.md`,
  `skills/local-worker/references/apple-verification.md`, `README.md`
- Test: `tests/test_plugin_package.py` (anchors), `tests/test_doctor.py`

**Interfaces:**
- Produces: `doctor [--json]` → `{"codex": {"version", "flags_ok"}, "omlx": discovery result or error code, "permissions": {"mode", "deny_list", "wrapper_available"}, "simulators": [{"udid", "name", "platform", "state"}], "state_dir", "plugin_version"}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_skill_teaches_the_loop_and_forbids_editing_permissions(self):
    text = (ROOT / "skills/local-worker/SKILL.md").read_text()
    for anchor in ("read job", "write job", "next_action", "escalate_cloud", "models --cloud",
                   "summary", "Suggested fixes for this plugin", "never edit permissions.json"):
        self.assertIn(anchor, text)
    for gone in ("Qwen3.8", "qwen-3.8", "tvOS simulator UUID", "global-policy.md"):
        self.assertNotIn(gone, text + (ROOT / "README.md").read_text())

def test_doctor_reports_every_section(self):
    out = self.run_main(["doctor", "--json"], omlx=sp.FakeOmlx("one"))
    self.assertEqual(sorted(out), ["codex", "omlx", "permissions", "plugin_version", "simulators", "state_dir"])
```

- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Write** the skill around the planner loop of §6.11 and the §11 output keys
  (decompose → read jobs → plan → write jobs → review patch/tests/screenshots → follow
  `next_action` → `summary`), the escalation rule (cheapest suitable model from `models --cloud`,
  low reasoning first), and the notification/summary hand-off. README: install, discovery,
  permissions, jobs, escalation, screenshots, summary, troubleshooting by classification code.
- [ ] **Step 4:** Run — PASS; full suite PASS.
- [ ] **Step 5:** Commit `docs: rewrite the skills and README for the universal worker`.

---

## Phase I — Qualification

### Task 26: Live qualification suite

**Files:**
- Create: `tests/live/test_live_qualification.py`, `scripts/prepare_read_pilot.py`,
  `docs/qualification/README.md`, `docs/qualification/<date>-<hostname>.md` (generated)

**Interfaces:**
- Consumes: everything above. Runs only when `LOCAL_WORKER_LIVE=1`; native and cloud parts only
  when `LOCAL_WORKER_LIVE_NATIVE_PLAN=<path>` / `LOCAL_WORKER_LIVE_CLOUD=1` are set.

- [ ] **Step 1: Write the suite** with these tests: `test_discovery_matches_server`,
  `test_read_pilot_citations_verified`, `test_write_pilot_ok_inside_wrapper`,
  `test_forced_context_overflow_is_environment` (`--context-cap 4096`),
  `test_forced_capability_failure_offers_escalation` (contract whose tests cannot pass),
  `test_cloud_escalation_argv` (dry: argv + catalog; real call only with `LOCAL_WORKER_LIVE_CLOUD=1`),
  `test_native_before_after_screenshots` (user-supplied plan), `test_summary_written`. Each test
  appends its outcome, timings and tokens to the generated qualification record.
- [ ] **Step 2:** Run on this Mac: `LOCAL_WORKER_LIVE=1 python3 -m unittest tests.live.test_live_qualification -v`
  — expected: all non-optional tests PASS; record committed.
- [ ] **Step 3:** Hand the same command to the user for the second Mac; its record is added when
  they run it.
- [ ] **Step 4:** Commit `test: add the live qualification suite and this Mac's record`.

---

## Phase J — Dashboard widget (later phase; deferred to future work)

### Task 27: Spike — MCP Apps widget in Codex desktop (gate) — DEFERRED

**Deferred — planned for future work (user decision 2026-10-25).** The v0.1.2 spike passed protocol tests and an isolated CLI app-server startup; desktop rendering was never observed (Computer Use policy blocks driving the Codex app) and no temporary plugin is installed now. The core CLI is the deliverable of this release.

**Files:**
- Create: `spikes/dashboard/server.py` (stdlib stdio MCP server), `spikes/dashboard/widget.html`,
  `docs/qualification/<date>-dashboard-spike.md`

- [ ] **Step 1:** Implement the minimal server: `initialize`, `tools/list` with one tool
  `open_dashboard` whose result carries `_meta.ui.resourceUri = "ui://local-worker/dashboard"`,
  `resources/read` returning `widget.html` with MIME `text/html;profile=mcp-app`; the widget uses
  the MCP Apps bridge (vendored like `creative-production`'s `ext-apps` build, or a minimal
  hand-written `ui/*` bridge) and shows a static table plus one embedded PNG.
- [ ] **Step 2:** Install via a temp marketplace in the Codex desktop app; record: does it render,
  which `availableDisplayModes` the host reports, whether `pip` works, how a data update reaches a
  mounted widget, and that the CLI ignores it.
- [ ] **Step 3:** Decide go/no-go in the record. No-go ⇒ stop here; Task 28 is dropped.
- [ ] **Step 4:** Commit `docs: record the dashboard widget spike`.

### Task 28: Dashboard widget (only after a go) — DEFERRED

**Deferred — planned for future work (user decision 2026-10-25).** The v0.1.2 spike passed protocol tests and an isolated CLI app-server startup; desktop rendering was never observed (Computer Use policy blocks driving the Codex app) and no temporary plugin is installed now. Not started; the core CLI is the deliverable of this release.

**Files:**
- Create: `src/dashboard/server.py`, `src/dashboard/widget.html`, `.mcp.json`
- Modify: `.codex-plugin/plugin.json` (`mcpServers: "./.mcp.json"`), `skills/local-worker/SKILL.md`
- Modify: `tests/support.py` (`tree(base: Path) -> Dict[str, Tuple[int, int]]`, moved from `test_plugin_package.py`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `ledger.build_summary` (Task 20).
- Produces: MCP tools `open_dashboard(session: Optional[str])` and `job_details(job_id: str)`,
  both read-only; widget renders jobs, tokens (local vs cloud), escalations, suggested fixes and
  screenshot sources side by side.

- [ ] **Step 1: Write the failing tests** — protocol-level, over stdio pipes:

```python
def test_open_dashboard_returns_the_widget_resource_and_summary_data(self):
    reply = self.call("tools/call", {"name": "open_dashboard", "arguments": {"session": "S1"}})
    self.assertEqual(reply["_meta"]["ui"]["resourceUri"], "ui://local-worker/dashboard")
    self.assertEqual(reply["structuredContent"]["session"], "S1")
    html = self.call("resources/read", {"uri": "ui://local-worker/dashboard"})["contents"][0]
    self.assertEqual(html["mimeType"], "text/html;profile=mcp-app")

def test_tools_never_change_state(self):
    before = sp.tree(self.state)
    self.call("tools/call", {"name": "job_details", "arguments": {"job_id": "J1"}})
    self.assertEqual(sp.tree(self.state), before)
```

- [ ] **Step 2:** Run — FAIL. **Step 3: Implement** per the spike's findings.
- [ ] **Step 4:** Run — PASS; verify in the desktop app; full suite PASS.
- [ ] **Step 5:** Commit `feat: add the session dashboard widget`.
