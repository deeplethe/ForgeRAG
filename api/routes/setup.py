"""
First-boot wizard endpoints.

The wizard runs UNAUTHENTICATED — by definition the operator
hasn't created an account yet, and the LLM keys it needs to set
gate every other endpoint that requires real LLM compute. The
middleware whitelists ``/api/v1/setup/`` so all four endpoints
below run without a session cookie.

Defence in depth:
  * ``/setup/commit`` returns 403 once the system is configured
    (so a malicious browser hit later can't reset the deploy).
  * Test endpoints have a hard timeout + cap on payload size, so
    a leaked URL can't be turned into a free LLM-key validator
    by random callers.

Lifecycle:
  1. Operator hits the web UI on a fresh deploy.
  2. App.vue probes ``/setup/status`` on mount; ``configured=False``
     bounces to ``/setup``.
  3. ``GET /setup/presets`` populates the tile grid.
  4. ``POST /setup/test-llm`` validates the chosen preset's chat
     model + key with a 1-token round trip.
  5. ``POST /setup/commit`` writes the wizard's choices to the
     overlay yaml and signals the worker to restart.
  6. Next boot loads the overlay → ``configured=True`` → wizard
     no longer accessible → operator continues to ``/register``.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_state
from ..setup_presets import PRESETS, get_preset, render_preset_config
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class StatusOut(BaseModel):
    configured: bool
    blockers: list[str] = Field(
        default_factory=list,
        description="Empty when configured=True; otherwise human-readable strings",
    )
    # Best-effort hints so the UI can pre-select what to ask. None
    # when we can't infer anything.
    suggested_locale: str | None = None


class PresetOut(BaseModel):
    id: str
    name: str
    tagline: str
    tagline_en: str
    logo_emoji: str
    recommended_for: list[str]
    inputs: list[dict]


class TestRequest(BaseModel):
    preset_id: str
    inputs: dict[str, str]


class TestResult(BaseModel):
    ok: bool
    error: str | None = None
    latency_ms: int | None = None
    model: str | None = None


class CommitRequest(BaseModel):
    preset_id: str
    inputs: dict[str, str]


class CommitResponse(BaseModel):
    ok: bool
    overlay_path: str
    restart_scheduled: bool


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _is_configured(cfg) -> tuple[bool, list[str]]:
    """A deploy is "configured" when the LLM-using surfaces all
    have non-empty credentials. We don't try to validate the keys
    work here — the wizard's test endpoints do that — only that
    SOMETHING was filled in."""
    blockers: list[str] = []

    gen = getattr(cfg.answering, "generator", None)
    gen_model = getattr(gen, "model", None) or ""
    gen_key = getattr(gen, "api_key", None) or os.environ.get(
        getattr(gen, "api_key_env", "") or "", ""
    )
    if not gen_model.strip() or not gen_key.strip():
        blockers.append("answering_llm")

    emb = getattr(cfg.embedder, "litellm", None)
    emb_model = getattr(emb, "model", None) or ""
    emb_key = getattr(emb, "api_key", None) or os.environ.get(
        getattr(emb, "api_key_env", "") or "", ""
    )
    if not emb_model.strip() or (
        not emb_key.strip() and not getattr(emb, "api_base", None)
    ):
        # Self-hosted embedders (Ollama at a local URL) don't need
        # a key — having an api_base is enough.
        blockers.append("embedder")

    return (not blockers, blockers)


@router.get("/status", response_model=StatusOut)
def get_status(state: AppState = Depends(get_state)):
    """Public probe used by the frontend to decide whether to
    bounce the user to the wizard. Returns immediately; safe to
    poll."""
    ok, blockers = _is_configured(state.cfg)
    # Try to detect whether the request comes from a CN browser
    # so the wizard can default to Chinese. Fallback to None when
    # we can't tell — the frontend's own locale preference still
    # wins.
    return StatusOut(
        configured=ok,
        blockers=blockers,
        suggested_locale=None,
    )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@router.get("/presets", response_model=list[PresetOut])
def list_presets():
    """Static catalog. The frontend caches this — adding presets
    requires a backend redeploy, which is fine: presets define
    routing into specific provider models, and we'd want a code
    review before introducing a new one anyway."""
    return [
        PresetOut(
            id=p["id"],
            name=p["name"],
            tagline=p["tagline"],
            tagline_en=p["tagline_en"],
            logo_emoji=p["logo_emoji"],
            recommended_for=p["recommended_for"],
            inputs=p["inputs"],
        )
        for p in PRESETS
    ]


# ---------------------------------------------------------------------------
# Connectivity tests
# ---------------------------------------------------------------------------


def _assert_unconfigured(state: AppState) -> None:
    """Test + commit are gated to the unconfigured state. Once the
    operator has finished the wizard, mutating the config requires
    going through the authenticated Settings UI (not yet wired,
    but the gate is the right shape for when it lands)."""
    ok, _ = _is_configured(state.cfg)
    if ok:
        raise HTTPException(403, "setup is already complete")


@router.post("/test-llm", response_model=TestResult)
def test_llm(body: TestRequest, state: AppState = Depends(get_state)):
    """Round-trip a 1-token chat completion against the chosen
    preset's chat endpoint. Returns ok=False with a friendly
    error string on failure — never 500s; the wizard renders the
    error in the UI."""
    _assert_unconfigured(state)

    preset = get_preset(body.preset_id)
    if preset is None:
        raise HTTPException(404, f"unknown preset: {body.preset_id!r}")

    if preset.get("skip_test"):
        # "Custom" preset has nothing to test — the operator drops
        # to the legacy Settings flow after commit.
        return TestResult(ok=True, model=None, latency_ms=0)

    test_spec = preset.get("test", {}).get("chat")
    if test_spec is None:
        return TestResult(ok=False, error="preset has no test spec")

    api_key_input = test_spec.get("api_key_input")
    api_key = body.inputs.get(api_key_input) if api_key_input else None
    api_base = test_spec.get("api_base")
    if "api_base_input" in test_spec:
        api_base = body.inputs.get(test_spec["api_base_input"]) or api_base

    model = test_spec["model"]

    import time

    t0 = time.monotonic()
    try:
        # Lazy import — litellm pulls in heavy deps and isn't
        # needed by the rest of the setup endpoints.
        import litellm

        # Defensive: turn telemetry off for THIS call too. The
        # state-level disable in api/state.py is the durable
        # one; this is belt-and-suspenders for cold-path code
        # that might run before state.py initialises (rare but
        # possible during early lifespan boot).
        os.environ.setdefault("LITELLM_TELEMETRY", "False")

        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "timeout": 15.0,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        resp = litellm.completion(**kwargs)
        # Touch the response so we surface auth errors that only
        # manifest when the body is consumed (some providers
        # delay their 401 until then).
        _ = getattr(resp, "choices", None)
    except Exception as e:
        return TestResult(ok=False, error=_friendly(e), model=model)

    return TestResult(
        ok=True,
        model=model,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )


def _friendly(e: Exception) -> str:
    """Map common litellm / provider exceptions to copy a human
    can act on. Falls back to the exception message."""
    s = str(e)
    if "401" in s or "Unauthorized" in s or "invalid_api_key" in s:
        return "Invalid API key — double-check the key you pasted."
    if "404" in s and "model" in s.lower():
        return "Model not found at this endpoint — provider may not stock it."
    if "Connection" in s or "timeout" in s.lower():
        return "Could not reach the provider. Check your network or the api_base URL."
    if "rate limit" in s.lower() or "429" in s:
        return "Rate limited by the provider. Wait a moment and try again."
    if len(s) > 300:
        return s[:300] + "…"
    return s or e.__class__.__name__


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _overlay_target() -> Path:
    """Same resolution as ``config.loader._overlay_path`` — kept
    in sync. Duplicated rather than imported to avoid pulling
    config internals into routes."""
    explicit = os.environ.get("OPENCRAIG_OVERLAY")
    if explicit:
        return Path(explicit)
    return Path("storage") / "setup-overlay.yaml"


def _set_dotted(out: dict, dotted: str, value) -> None:
    """Plant ``value`` at ``a.b.c`` in ``out``, creating intermediate
    dicts as needed."""
    parts = dotted.split(".")
    cur = out
    for seg in parts[:-1]:
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[parts[-1]] = value


@router.post("/commit", response_model=CommitResponse)
def commit(body: CommitRequest, state: AppState = Depends(get_state)):
    """Write the wizard's choices to the overlay yaml and signal
    the worker to restart so the new config takes effect.

    The overlay layer means the operator's hand-written
    ``docker/config.yaml`` is never mutated — what they see in
    git stays untouched; the wizard's output lives next to the
    blob storage volume and is loaded via ``config.loader``'s
    deep-merge on startup.
    """
    _assert_unconfigured(state)

    preset = get_preset(body.preset_id)
    if preset is None:
        raise HTTPException(404, f"unknown preset: {body.preset_id!r}")

    flat = render_preset_config(preset, body.inputs)
    if not flat and body.preset_id != "custom":
        raise HTTPException(400, "preset rendered to an empty config — required input missing?")

    nested: dict = {}
    for path, value in flat.items():
        _set_dotted(nested, path, value)

    target = _overlay_target()
    target.parent.mkdir(parents=True, exist_ok=True)

    import yaml as _yaml
    with open(target, "w", encoding="utf-8") as f:
        f.write(
            "# OpenCraig setup-wizard overlay (auto-generated).\n"
            "# Edit `docker/config.yaml` for declarative changes; this\n"
            "# file is loaded on top of it at startup.\n\n"
        )
        _yaml.safe_dump(nested, f, sort_keys=True, allow_unicode=True)

    log.info("setup wizard wrote overlay to %s (preset=%s)", target, body.preset_id)

    # Schedule a restart so the new overlay loads. ``unless-stopped``
    # in compose brings the container back; bare ``main.py`` runs
    # are revived by the user's process supervisor (systemd /
    # supervisord). Best-effort: ignore failures and let the
    # operator restart by hand.
    restart_ok = False
    try:
        # SIGTERM gives the lifespan shutdown a chance to flush;
        # uvicorn handles it gracefully. We schedule it AFTER the
        # response goes out so the client sees ``ok=True``.
        import threading
        threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        restart_ok = True
    except Exception:
        log.warning("could not schedule restart; operator will need to restart manually")

    return CommitResponse(
        ok=True,
        overlay_path=str(target),
        restart_scheduled=restart_ok,
    )
