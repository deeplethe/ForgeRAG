"""LiteLLM success callback that funnels per-call token usage back into
the active agent run's budget tracker.

Why this layer (not the SDK):
  * The Claude Agent SDK ``AssistantMessage.usage`` is populated only by
    Anthropic-native providers; LiteLLM-bridged providers (DeepSeek,
    OpenAI, Bedrock, ...) bubble usage up only on the final
    ``ResultMessage`` — which fires AFTER the run is over, defeating
    mid-run budget enforcement.
  * LiteLLM normalises ALL provider responses into a ``ModelResponse``
    with a uniform ``usage`` dict (``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens``). Registering the callback
    once gets us per-call usage regardless of upstream provider —
    Anthropic, DeepSeek, OpenAI, Bedrock, vLLM, ollama, ... — without
    any provider-specific glue.

Registration: ``init_litellm_usage_callback(app_state)`` is called once
from the FastAPI lifespan startup. It captures a weak reference to the
AppState so the callback can look up ``app_state.active_runs`` at call
time.

Wire path:
  agent (in container) → POST /llm_proxy/anthropic →
  litellm.anthropic.messages.acreate(...) →
  [response streams back to agent] →
  litellm fires success_callback with (kwargs, response, start, end) →
  THIS callback resolves user_id → finds active run handle →
  handle.add_usage(prompt, completion) →
  if over budget → emit hard budget_warning + flip
  handle._budget_exhausted; the proxy's pre-flight check then refuses
  the agent's NEXT call with 402, which the SDK treats as upstream
  failure and the run cleanly terminates.

Provider neutrality is the load-bearing property: do NOT add
provider-specific parsing here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


_APP_STATE_REF: Any | None = None


_LOGGER_INSTANCE: Any | None = None


def init_litellm_usage_callback(app_state: Any) -> None:
    """Register the per-LLM-call usage callback with LiteLLM. Idempotent
    — safe to call multiple times during boot.

    Registers via TWO routes for robustness:
      1. ``litellm.success_callback.append(func)`` — fires for plain
         ``litellm.completion`` paths. Works for the ``/v1/llm_proxy``
         OpenAI-shaped route + the reranker call.
      2. ``litellm.callbacks.append(CustomLogger)`` — required for the
         Anthropic adapter path (``litellm.anthropic.messages.acreate``)
         which routes telemetry through CustomLogger.log_success_event
         instead of the function-style callback. This is how the
         in-container agent's LLM calls actually arrive.
    """
    global _APP_STATE_REF, _LOGGER_INSTANCE
    _APP_STATE_REF = app_state
    try:
        import litellm  # type: ignore[import-not-found]
    except ImportError:
        log.warning("litellm not importable — usage callback NOT registered")
        return
    existing = list(litellm.success_callback or [])
    if _on_litellm_success not in existing:
        existing.append(_on_litellm_success)
        litellm.success_callback = existing

    # CustomLogger route — covers the Anthropic adapter path used by
    # the in-container agent. Without this, LLM calls from the SDK
    # bypass our callback and budget enforcement never sees usage.
    try:
        from litellm.integrations.custom_logger import (  # type: ignore[import-not-found]
            CustomLogger,
        )
    except ImportError:
        CustomLogger = None  # type: ignore[assignment]
    if CustomLogger is not None and _LOGGER_INSTANCE is None:

        class _OpenCraigUsageLogger(CustomLogger):  # type: ignore[misc, valid-type]
            def log_success_event(
                self, kwargs, response_obj, start_time, end_time
            ):
                _on_litellm_success(
                    kwargs, response_obj, start_time, end_time
                )

            async def async_log_success_event(
                self, kwargs, response_obj, start_time, end_time
            ):
                _on_litellm_success(
                    kwargs, response_obj, start_time, end_time
                )

        _LOGGER_INSTANCE = _OpenCraigUsageLogger()
        cbs = list(getattr(litellm, "callbacks", None) or [])
        if _LOGGER_INSTANCE not in cbs:
            cbs.append(_LOGGER_INSTANCE)
            litellm.callbacks = cbs

    log.info(
        "litellm_usage_callback: registered "
        "success_callbacks=%d callbacks=%d",
        len(existing),
        len(getattr(litellm, "callbacks", None) or []),
    )


def _on_litellm_success(
    kwargs: dict[str, Any],
    response_obj: Any,
    start_time: Any,
    end_time: Any,
) -> None:
    """LiteLLM success-callback function form. Fires after each LLM call
    (streaming completes or non-streaming returns).

    LiteLLM passes:
      kwargs        — the original request dict (model, messages, metadata,
                      ...). user_id is expected at ``kwargs['metadata']``.
      response_obj  — provider-normalised response with ``usage``.
      start_time    — datetime
      end_time      — datetime
    """
    # Debug log left at DEBUG so the per-call hook stays quiet in
    # production but the wire is inspectable when needed.
    log.debug(
        "usage_callback: fired model=%s",
        kwargs.get("model"),
    )
    state = _APP_STATE_REF
    if state is None:
        return  # not initialised; quietly no-op rather than crash a LLM call

    # Provider-agnostic usage extraction. LiteLLM's ModelResponse exposes
    # ``usage`` as either an object (with .prompt_tokens etc) or a dict.
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return  # provider didn't report usage — nothing to credit

    def _g(name: str) -> int:
        v = getattr(usage, name, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(name)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _g("prompt_tokens") or _g("input_tokens")
    completion_tokens = _g("completion_tokens") or _g("output_tokens")
    if prompt_tokens == 0 and completion_tokens == 0:
        return

    # Identify the active run. LiteLLM puts logging metadata in
    # ``kwargs["litellm_params"]["litellm_metadata"]`` for the
    # Anthropic adapter path; the OpenAI / completion path puts user
    # tags directly in ``kwargs["metadata"]``. Check both, plus a few
    # standard fallbacks LiteLLM versions have used.
    user_id = None
    lp = kwargs.get("litellm_params") or {}
    if isinstance(lp, dict):
        lm = lp.get("litellm_metadata") or {}
        if isinstance(lm, dict):
            user_id = lm.get("opencraig_user_id")
    if not user_id:
        md = kwargs.get("metadata") or {}
        if isinstance(md, dict):
            user_id = md.get("opencraig_user_id") or md.get("user_id")
    if not user_id:
        slo = kwargs.get("standard_logging_object") or {}
        if isinstance(slo, dict):
            slm = slo.get("metadata") or {}
            if isinstance(slm, dict):
                user_id = slm.get("opencraig_user_id")
    if not user_id:
        user_id = kwargs.get("user")
    if not user_id:
        return

    # Lookup: scan the small active_runs dict for the user. Active runs
    # are bounded by concurrent users (typically tens), so the linear
    # scan is fine — index can be added if it becomes hot.
    handle = None
    try:
        for h in list(state.active_runs.values()):
            if getattr(h, "user_id", None) == user_id:
                handle = h
                break
    except Exception:
        log.exception("usage_callback: active_runs scan failed")
        return
    if handle is None:
        return  # LLM call from a user without an active managed run — fine

    try:
        handle.add_usage(prompt_tokens, completion_tokens)
    except Exception:
        log.exception("usage_callback: add_usage failed")
        return

    # Emit usage + check budget. handle.emit is async but the LiteLLM
    # callback is sync; schedule on the handle's loop via run_coroutine_
    # threadsafe so we don't block the LLM thread.
    loop = getattr(handle, "loop", None)
    if loop is None:
        # No captured loop (shouldn't happen post round-7 fix in
        # task_handle.start()) — totals are already updated on the
        # handle; skip the emit + budget signal.
        log.warning("usage_callback: handle has no loop; emit skipped")
        return
    coro = handle.emit(
        "usage",
        {
            "input_tokens": handle.total_input_tokens,
            "output_tokens": handle.total_output_tokens,
        },
    )
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        log.exception("usage_callback: run_coroutine_threadsafe emit failed")

    if handle.is_over_budget():
        handle._budget_exhausted = True
        log.warning(
            "usage_callback: budget exhausted user=%s total=%d limit=%s",
            user_id,
            handle.total_input_tokens + handle.total_output_tokens,
            handle.token_budget_total,
        )
