"""Ask the local oMLX server which model is loaded and what its limits are.

Nothing about the model is hardcoded: ``discover`` reads ``~/.omlx/settings.json`` for the
endpoint and key, then asks the server (``/health``, ``/v1/models/status``, ``/api/status``)
which chat model is loaded, its effective context window, output cap and vision support.
The only fallback is oMLX's own default port, recorded as ``port_source: "default"``.

Standard library only. The endpoint is loopback only; proxies and redirects are refused; the
API key is sent as a bearer header only where oMLX requires it and is never placed in an
exception message, a repr or a report.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

CHAT_MODEL_TYPES = frozenset({"llm", "vlm"})
DEFAULT_PORT = 8000
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
HTTP_TIMEOUT_SECONDS = 15
# Loading is a blocking call that returns once the weights are in memory, which takes far
# longer than a status read; only that one request gets the long allowance.
LOAD_TIMEOUT_SECONDS = 900
# A throughput average over a handful of requests says nothing about the server.
MIN_PREFILL_SAMPLE_TOKENS = 10000
# A model id or alias is later written inside a quoted TOML value and an HTTP path, so only a
# conservative character set is trusted. Always used with ``fullmatch`` (``$`` lets "\n" pass).
SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
# The bearer key becomes a header value: printable ASCII without spaces keeps it that.
SAFE_API_KEY = re.compile(r"[\x21-\x7e]+")
_LOAD_PATH = re.compile(r"/v1/models/[^/?#]+/load")


class DiscoveryError(Exception):
    """A discovery step failed; ``code`` says which, ``candidates`` lists model names to choose."""

    def __init__(self, code: str, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.candidates: Tuple[str, ...] = tuple(candidates)


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    api_key: str = field(repr=False)
    model_fallback: Optional[bool]
    port_source: str

    @property
    def base_url(self) -> str:
        host = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d" % (host, self.port)


@dataclass(frozen=True)
class ModelProfile:
    canonical_id: str
    api_model: str
    model_type: str
    engine_type: Optional[str]
    context_window: int
    max_output_tokens: Optional[int]
    accepts_images: bool
    thinking_default: Optional[bool]
    reasoning_efforts: Optional[List[str]]


@dataclass(frozen=True)
class ServerProfile:
    base_url: str
    version: Optional[str]
    model: ModelProfile
    prefill_tps: Optional[float]
    generation_tps: Optional[float]
    server_prompt_tokens: Optional[int]
    active_requests: Optional[int]
    discovered_at: str

    def as_report(self) -> Dict[str, Any]:
        """Plain JSON-ready facts about the server and the pinned model (no credentials)."""
        return asdict(self)

    def effective_context(self, cap: Optional[int]) -> int:
        """The server's window, or a lower caller-chosen cap; a cap never raises it."""
        if cap is None:
            return self.model.context_window
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError("context cap must be a positive integer, got %r" % (cap,))
        return min(self.model.context_window, cap)


HttpFetch = Callable[[ServerSettings, str, bool], Tuple[int, Any]]


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------
def _invalid(message: str) -> DiscoveryError:
    return DiscoveryError("settings_invalid", message)


def read_server_settings(path: Path) -> ServerSettings:
    """Endpoint, key and fallback flag from oMLX's ``settings.json``; loopback hosts only."""
    path = Path(path).expanduser()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _invalid("oMLX settings not found at %s" % path) from None
    except (OSError, ValueError) as error:
        raise _invalid("cannot read oMLX settings at %s (%s)" % (
            path, error.__class__.__name__)) from None
    if not isinstance(loaded, dict):
        raise _invalid("oMLX settings at %s must contain a JSON object" % path)

    auth = loaded.get("auth") if isinstance(loaded.get("auth"), dict) else {}
    key = auth.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise _invalid("%s has no auth.api_key; refusing to continue" % path)
    key = key.strip()
    if not SAFE_API_KEY.fullmatch(key):
        raise _invalid("auth.api_key in %s contains characters that cannot be sent as an "
                       "HTTP header value" % path)

    server = loaded.get("server") if isinstance(loaded.get("server"), dict) else {}
    host = server.get("host")
    if not isinstance(host, str) or not host.strip():
        raise _invalid("%s has no server.host; refusing to guess an endpoint" % path)
    if host not in LOOPBACK_HOSTS:
        raise DiscoveryError("not_loopback", "oMLX server.host is %r; only loopback hosts (%s) "
                             "are allowed" % (host, ", ".join(LOOPBACK_HOSTS)))

    port = server.get("port")
    if port is None:
        port, port_source = DEFAULT_PORT, "default"
    elif isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise _invalid("server.port in %s must be an integer from 1 to 65535, got %r" % (
            path, port))
    else:
        port_source = "settings"

    model = loaded.get("model") if isinstance(loaded.get("model"), dict) else {}
    fallback = model.get("model_fallback")
    return ServerSettings(host=host, port=port, api_key=key,
                          model_fallback=fallback if isinstance(fallback, bool) else None,
                          port_source=port_source)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx must surface as an error, never be followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """Opener with environment proxies disabled and redirects refused."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def _decode_body(raw: bytes) -> Any:
    """Parsed JSON, ``None`` for an empty body; ``ValueError`` when the body is not JSON."""
    if not raw.strip():
        return None
    return json.loads(raw.decode("utf-8"))


def http_fetch(settings: ServerSettings, path: str, auth: bool,
               timeout: Optional[float] = None) -> Tuple[int, Any]:
    """One request to the oMLX server: ``(status, parsed JSON body)``.

    GET, except the one blocking model-load endpoint, which is a POST. No proxies, no
    redirects, 15 s timeout. The bearer key is attached only when ``auth`` is true. Connection
    failures and redirects raise ``server_unreachable``; 401 and 403 raise ``auth_failed``;
    every other status is returned with its body (``None`` when it is not JSON) so callers can
    read, for example, the 503 ``loading`` health answer.
    """
    loading = bool(_LOAD_PATH.fullmatch(path))
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + settings.api_key
    request = urllib.request.Request(settings.base_url + path, headers=headers,
                                     method="POST" if loading else "GET")
    timeout = (LOAD_TIMEOUT_SECONDS if loading else HTTP_TIMEOUT_SECONDS) \
        if timeout is None else max(.001, float(timeout))
    try:
        with build_opener().open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            raw = error.read()
        except Exception:  # an unreadable error body only loses detail
            raw = b""
        finally:
            error.close()
        if status in (401, 403):
            raise DiscoveryError(
                "auth_failed", "oMLX rejected the API key (HTTP %d on %s)" % (status, path)
            ) from None
        if 300 <= status < 400:
            raise DiscoveryError(
                "server_unreachable", "oMLX answered %s with a redirect (HTTP %d); redirects "
                "are refused" % (path, status)) from None
    except Exception as error:  # URLError, timeouts, resets; never echo the raw text
        reason = getattr(error, "reason", error)
        detail = getattr(reason, "strerror", None)
        raise DiscoveryError(
            "server_unreachable", "cannot reach oMLX at %s (%s%s); is it running?" % (
                settings.base_url, reason.__class__.__name__, ": %s" % detail if detail else "")
        ) from None
    try:
        return status, _decode_body(raw)
    except ValueError:
        if 200 <= status < 300:
            raise DiscoveryError(
                "server_unreachable", "oMLX answered %s with a body that is not JSON" % path
            ) from None
        return status, None


# --------------------------------------------------------------------------------------
# Model selection and profile
# --------------------------------------------------------------------------------------
def _entries(models_status: Any) -> List[Dict[str, Any]]:
    models = models_status.get("models") if isinstance(models_status, dict) else None
    return [m for m in models or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _alias(entry: Dict[str, Any]) -> Optional[str]:
    alias = entry.get("model_alias")
    return alias if isinstance(alias, str) and alias else None


def _is_chat(entry: Dict[str, Any]) -> bool:
    """Text and vision LLMs that are not helper models (embedding, rerank, STT excluded)."""
    return not entry.get("is_helper") and entry.get("model_type") in CHAT_MODEL_TYPES


def _is_candidate(entry: Dict[str, Any]) -> bool:
    return entry.get("loaded") is True and _is_chat(entry)


def _names(entry: Dict[str, Any]) -> Tuple[str, ...]:
    return (entry["id"],) + ((_alias(entry),) if _alias(entry) else ())


def _positive_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def select_model(models_status: Dict, health: Dict, requested: Optional[str]) -> Dict:
    """The one loaded chat model to use, as its ``/v1/models/status`` entry.

    Candidates are loaded, non-helper models of a chat type. A requested name matches an id
    (preferred) or an alias and must be a candidate; otherwise a single candidate wins, then
    oMLX's ``default_model``; anything else is ambiguous.
    """
    entries = _entries(models_status)
    candidates = [m for m in entries if _is_candidate(m)]
    loaded_ids = tuple(m["id"] for m in candidates)
    loadable = tuple(m["id"] for m in entries if _is_chat(m) and m.get("loaded") is not True)

    if requested is not None:
        for matches in ([m for m in candidates if m["id"] == requested],
                        [m for m in candidates if _alias(m) == requested]):
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise DiscoveryError(
                    "model_ambiguous", "%r matches several loaded models; use a canonical id"
                    % requested, tuple(m["id"] for m in matches))
        known = [m for m in entries if requested in _names(m)]
        if any(_is_chat(m) for m in known):
            raise DiscoveryError(
                "model_not_loaded", "%r is discovered but not loaded; load it first (the "
                "launcher's --load does that)" % requested, loadable)
        if known:
            raise DiscoveryError(
                "model_unknown", "%r is a helper or non-chat model and cannot run a worker; "
                "loaded chat models: %s" % (requested, ", ".join(loaded_ids) or "none"),
                loaded_ids)
        raise DiscoveryError(
            "model_unknown", "oMLX has no model %r; loaded chat models: %s" % (
                requested, ", ".join(loaded_ids) or "none"), loaded_ids)

    if not candidates:
        loading = [m["id"] for m in entries if _is_chat(m) and m.get("is_loading")]
        note = "; still loading: %s" % ", ".join(loading) if loading else ""
        raise DiscoveryError(
            "model_not_loaded", "no chat model is loaded in oMLX%s; models that could be "
            "loaded: %s" % (note, ", ".join(loadable) or "none"), loadable)
    if len(candidates) == 1:
        return candidates[0]
    default = health.get("default_model") if isinstance(health, dict) else None
    if isinstance(default, str) and default:
        chosen = [m for m in candidates if default in _names(m)]
        if len(chosen) == 1:
            return chosen[0]
    raise DiscoveryError(
        "model_ambiguous", "several chat models are loaded and none is the server default; "
        "name one explicitly: %s" % ", ".join(loaded_ids), loaded_ids)


def _shown(value: str) -> str:
    return repr(value if len(value) <= 80 else value[:80] + "...")


def _require_safe(entry: Dict[str, Any], api_model: str) -> None:
    for label, name in (("canonical model id", entry["id"]), ("model alias", api_model)):
        if not SAFE_MODEL_NAME.fullmatch(name):
            raise DiscoveryError(
                "model_unknown", "oMLX reported %s %s, which is not a safe model name "
                "(letters, digits and . _ : / @ + - only, at most 128 characters); refusing "
                "to use it in a launch command" % (label, _shown(name)))


def _build_model(entry: Dict[str, Any]) -> ModelProfile:
    api_model = _alias(entry) or entry["id"]
    _require_safe(entry, api_model)
    window = _positive_int(entry.get("max_context_window"))
    if window is None:
        raise DiscoveryError(
            "model_unknown", "oMLX did not report max_context_window for %s; the effective "
            "context cannot be determined" % _shown(entry["id"]))
    efforts = entry.get("reasoning_efforts")
    thinking = entry.get("thinking_default")
    model_type, engine_type = entry.get("model_type"), entry.get("engine_type")
    return ModelProfile(
        canonical_id=entry["id"], api_model=api_model, model_type=model_type,
        engine_type=engine_type if isinstance(engine_type, str) else None,
        context_window=window, max_output_tokens=_positive_int(entry.get("max_tokens")),
        accepts_images=model_type == "vlm" or engine_type == "vlm",
        thinking_default=thinking if isinstance(thinking, bool) else None,
        reasoning_efforts=([e for e in efforts if isinstance(e, str)]
                           if isinstance(efforts, list) else None))


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------
def _remaining(deadline: Optional[Callable[[], float]]) -> Optional[float]:
    if deadline is None:
        return None
    remaining = float(deadline())
    if remaining <= 0:
        raise DiscoveryError("budget_exhausted", "job deadline expired before the next oMLX request")
    return remaining


def _deadline_fetch(fetch: HttpFetch, settings: ServerSettings, path: str, auth: bool,
                    deadline: Optional[Callable[[], float]]) -> Tuple[int, Any]:
    remaining = _remaining(deadline)
    if remaining is not None and fetch is http_fetch:
        return fetch(settings, path, auth, timeout=remaining)
    return fetch(settings, path, auth)


def _read_models_status(fetch: HttpFetch, settings: ServerSettings,
                        deadline: Optional[Callable[[], float]] = None) -> Dict[str, Any]:
    status, body = _deadline_fetch(fetch, settings, "/v1/models/status", True, deadline)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("models"), list):
        raise DiscoveryError(
            "server_unreachable", "unexpected /v1/models/status answer from oMLX (HTTP %s)"
            % status)
    return body


def discover(settings_path: Path, requested_model: Optional[str] = None,
             fetch: HttpFetch = http_fetch,
             deadline: Optional[Callable[[], float]] = None) -> ServerProfile:
    """Everything the launcher needs to know about the live oMLX server and its loaded model."""
    settings = read_server_settings(settings_path)
    if settings.model_fallback is not False:
        raise DiscoveryError(
            "model_fallback_enabled", "oMLX model.model_fallback must be false (it is %r); "
            "with fallback on, oMLX may serve a different model than the one pinned"
            % (settings.model_fallback,))

    status, health = _deadline_fetch(fetch, settings, "/health", False, deadline)
    health_status = health.get("status") if isinstance(health, dict) else None
    if health_status == "loading":
        raise DiscoveryError("server_loading", "oMLX is still loading (/health reports "
                             "'loading'); retry once it is healthy")
    if status != 200 or health_status != "healthy":
        raise DiscoveryError("server_unreachable", "oMLX /health is not healthy (HTTP %s, "
                             "status %r)" % (status, health_status))

    models_status = _read_models_status(fetch, settings, deadline)
    entry = select_model(models_status, health, requested_model)
    model = _build_model(entry)

    # Throughput and version are context only: an unavailable /api/status loses them, nothing
    # else. That covers a non-200 answer, a malformed body and a fetch that raises (reset,
    # timeout, 2xx body that is not JSON), because /health and the model list already succeeded.
    try:
        api_status, api = _deadline_fetch(fetch, settings, "/api/status", True, deadline)
    except DiscoveryError as error:
        if error.code == "budget_exhausted":
            raise
        api_status, api = 0, {}
    api = api if api_status == 200 and isinstance(api, dict) else {}
    prompt_tokens = api.get("total_prompt_tokens")
    prompt_tokens = prompt_tokens if isinstance(prompt_tokens, int) \
        and not isinstance(prompt_tokens, bool) else None
    version = api.get("version")
    active = api.get("active_requests")
    return ServerProfile(
        base_url=settings.base_url,
        version=version if isinstance(version, str) else None,
        model=model,
        prefill_tps=(_number(api.get("avg_prefill_tps"))
                     if prompt_tokens is not None and prompt_tokens >= MIN_PREFILL_SAMPLE_TOKENS
                     else None),
        generation_tps=_number(api.get("avg_generation_tps")),
        server_prompt_tokens=prompt_tokens,
        active_requests=active if isinstance(active, int) and not isinstance(active, bool)
        else None,
        discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def recheck(profile: ServerProfile, settings_path: Path,
            fetch: HttpFetch = http_fetch,
            deadline: Optional[Callable[[], float]] = None) -> Optional[str]:
    """Is the pinned model still what oMLX serves? ``None`` if so, else why the attempt must not
    start: ``"model_changed"`` (gone, unloaded, renamed, context shrank, fallback switched on) or
    ``"server_unreachable"`` (no usable answer, or the settings no longer point at the pinned
    endpoint)."""
    try:
        settings = read_server_settings(settings_path)
        if settings.base_url != profile.base_url:
            return "server_unreachable"
        if settings.model_fallback is not False:
            return "model_changed"
        models_status = _read_models_status(fetch, settings, deadline)
    except DiscoveryError as error:
        if error.code == "budget_exhausted":
            return "budget_exhausted"
        return "server_unreachable"
    pinned = profile.model
    for entry in _entries(models_status):
        if entry["id"] != pinned.canonical_id:
            continue
        window = _positive_int(entry.get("max_context_window"))
        if (_is_candidate(entry) and (_alias(entry) or entry["id"]) == pinned.api_model
                and window is not None and window >= pinned.context_window):
            return None
        break
    return "model_changed"


def load_model(settings_path: Path, model_id: str, fetch: HttpFetch = http_fetch,
               timeout: Optional[float] = None) -> None:
    """Ask oMLX to load a discovered model (the call blocks until it is in memory)."""
    settings = read_server_settings(settings_path)
    if not SAFE_MODEL_NAME.fullmatch(model_id):
        raise DiscoveryError("model_unknown", "%s is not a safe model name; refusing to ask "
                             "oMLX to load it" % _shown(model_id))
    path = "/v1/models/%s/load" % urllib.parse.quote(model_id, safe="")
    if timeout is not None and fetch is http_fetch:
        status, body = fetch(settings, path, True, timeout=timeout)
    else:
        status, body = fetch(settings, path, True)
    if status == 200:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    reason = " (%s)" % detail[:200] if isinstance(detail, str) and detail else ""
    code = "model_unknown" if status == 404 else "model_not_loaded"
    raise DiscoveryError(code, "oMLX could not load %s: HTTP %s%s" % (_shown(model_id), status,
                                                                      reason))
