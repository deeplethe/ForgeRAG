"""
LLM-backed answer generator.

The Generator takes a pre-built message list (from prompts.py) and
returns the raw answer text, the cited marker ids, and LLM metadata.
Wrapping the litellm call in a Protocol means tests can inject a
fake generator without touching network code.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Protocol

from config import GeneratorConfig

from .prompts import extract_cited_ids

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol + factory
# ---------------------------------------------------------------------------


class Generator(Protocol):
    backend: str
    model: str

    def generate(self, messages: list[dict]) -> dict:
        """
        Return a dict with keys:
            text:          str
            finish_reason: str
            usage:         dict | None
            model:         str
            cited_ids:     list[str]   (parsed from text)
        """
        ...

    def generate_stream(self, messages: list[dict]):
        """
        Yield dicts with keys:
            type:   "delta" | "done"
            delta:  str  (text fragment, only for type="delta")
            text:   str  (full text, only for type="done")
            finish_reason: str (only for type="done")
            usage:  dict | None (only for type="done")
            model:  str
            cited_ids: list[str] (only for type="done")
        """
        ...


def make_generator(cfg: GeneratorConfig) -> Generator:
    if cfg.backend == "litellm":
        return LiteLLMGenerator(cfg)
    raise ValueError(f"unknown generator backend: {cfg.backend!r}")


# ---------------------------------------------------------------------------
# LiteLLM implementation
# ---------------------------------------------------------------------------


class LiteLLMGenerator:
    backend = "litellm"

    def __init__(self, cfg: GeneratorConfig):
        self.cfg = cfg
        self.model = cfg.model
        self._litellm = None

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._litellm is not None:
            return self._litellm
        try:
            import litellm
        except ImportError as e:
            raise RuntimeError("LiteLLMGenerator requires litellm: pip install litellm") from e
        from config.auth import resolve_api_key

        self._api_key = resolve_api_key(
            api_key=self.cfg.api_key,
            api_key_env=self.cfg.api_key_env,
            required=False,
            context="answering.generator",
        )
        self._litellm = litellm
        return litellm

    # ------------------------------------------------------------------
    def generate(self, messages: list[dict], *, overrides: Any = None) -> dict:
        litellm = self._ensure()
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            timeout=self.cfg.timeout,
        )
        # Only forward ``max_tokens`` when the user explicitly set one.
        # Default (None) lets the provider use the model's own maximum,
        # which matters for thinking-mode models that count reasoning
        # tokens against the cap.
        if self.cfg.max_tokens is not None and self.cfg.max_tokens > 0:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base

        if self._api_key:
            kwargs["api_key"] = self._api_key

        # Reasoning controls — LiteLLM handles the per-provider routing,
        # so we just forward the typed fields verbatim and let it translate.
        if self.cfg.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.cfg.reasoning_effort
        if self.cfg.thinking is not None:
            kwargs["thinking"] = self.cfg.thinking
        # Escape hatch for everything else (top_p, extra_body, ...).
        # Applied last so it can override the typed fields above.
        if self.cfg.extra_kwargs:
            kwargs.update(self.cfg.extra_kwargs)

        # Per-request overrides win over yaml. UI-level "Tools" panel
        # uses these for reasoning_effort / temperature / max_tokens.
        _apply_gen_overrides(kwargs, overrides)

        max_retries = getattr(self.cfg, "max_retries", 3)
        retry_delay = getattr(self.cfg, "retry_base_delay", 1.0)
        last_err = None
        t0 = time.time()
        for attempt in range(max_retries):
            try:
                resp = litellm.completion(**kwargs)
                text = _extract_text(resp)
                finish = _extract_finish_reason(resp)
                usage = _extract_usage(resp)
                cited = extract_cited_ids(text)

                return {
                    "text": text,
                    "finish_reason": finish,
                    "usage": usage,
                    "model": self.cfg.model,
                    "cited_ids": cited,
                    "latency_ms": int((time.time() - t0) * 1000),
                }
            except Exception as e:
                last_err = e
                log.warning(
                    "generator LLM call attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2**attempt))

        log.error("generator LLM call failed after %d retries: %s", max_retries, last_err)
        return {
            "text": "",
            "finish_reason": "error",
            "usage": None,
            "model": self.cfg.model,
            "cited_ids": [],
            "error": str(last_err),
            "latency_ms": int((time.time() - t0) * 1000),
        }

    # ------------------------------------------------------------------
    def generate_stream(self, messages: list[dict], *, overrides: Any = None):
        litellm = self._ensure()
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            timeout=self.cfg.timeout,
            stream=True,
        )
        # See ``generate`` — only forward when explicitly configured.
        if self.cfg.max_tokens is not None and self.cfg.max_tokens > 0:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base
        # Reasoning controls — see ``generate`` for explanation.
        if self.cfg.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.cfg.reasoning_effort
        if self.cfg.thinking is not None:
            kwargs["thinking"] = self.cfg.thinking
        if self.cfg.extra_kwargs:
            kwargs.update(self.cfg.extra_kwargs)
        _apply_gen_overrides(kwargs, overrides)

        full_text = ""
        full_thinking = ""
        try:
            resp = litellm.completion(**kwargs)
            finish_reason = "stop"
            for chunk in resp:
                delta = ""
                # Reasoning models (DeepSeek V4-Pro thinking mode,
                # deepseek-reasoner, OpenAI o1, etc.) stream the model's
                # internal reasoning under ``delta.reasoning_content``
                # alongside the user-visible ``delta.content``. We
                # forward both so the UI can show "Thinking… ▾" panes.
                thinking = ""
                with contextlib.suppress(AttributeError, IndexError):
                    delta = chunk.choices[0].delta.content or ""
                with contextlib.suppress(AttributeError, IndexError):
                    thinking = chunk.choices[0].delta.reasoning_content or ""
                fr = (
                    getattr(chunk.choices[0], "finish_reason", None)
                    if getattr(chunk, "choices", None) and len(chunk.choices) > 0
                    else None
                )
                if fr:
                    finish_reason = fr
                if thinking:
                    full_thinking += thinking
                    yield {
                        "type": "thinking",
                        "delta": thinking,
                        "model": self.cfg.model,
                    }
                if delta:
                    full_text += delta
                    yield {
                        "type": "delta",
                        "delta": delta,
                        "model": self.cfg.model,
                    }

            cited = extract_cited_ids(full_text)
            yield {
                "type": "done",
                "text": full_text,
                "thinking": full_thinking,
                "finish_reason": finish_reason,
                "usage": None,
                "model": self.cfg.model,
                "cited_ids": cited,
            }
        except Exception as e:
            log.error("generator stream failed: %s", e)
            yield {
                "type": "done",
                "text": full_text,
                "finish_reason": "error",
                "usage": None,
                "model": self.cfg.model,
                "cited_ids": extract_cited_ids(full_text),
                "error": str(e),
            }


# ---------------------------------------------------------------------------
# Per-request override application
# ---------------------------------------------------------------------------


def _apply_gen_overrides(kwargs: dict, overrides: Any) -> None:
    """Apply ``GenerationOverrides`` (or a plain dict) to LiteLLM kwargs.

    Accepts either a pydantic model with attribute access or a plain
    dict — the latter lets internal callers pass overrides without
    importing the schema. ``None`` / unset fields are no-ops; explicit
    values WIN over yaml-level cfg.
    """
    if overrides is None:
        return
    # Coerce to a plain dict of {field: value} for unset-aware iteration.
    if hasattr(overrides, "model_dump"):
        data = overrides.model_dump(exclude_none=True)
    elif isinstance(overrides, dict):
        data = {k: v for k, v in overrides.items() if v is not None}
    else:
        return

    if "thinking" in data:
        # Boolean toggle. Route both via DeepSeek's extra_body channel
        # (the only way to actually disable on V4-Pro) AND via LiteLLM's
        # ``reasoning_effort`` (covers Anthropic / Gemini "off" semantics,
        # ignored by providers that don't recognize it). For "On" we
        # only set extra_body — intensity is up to ``reasoning_effort``
        # below, so we don't overwrite a user-picked level.
        on = bool(data["thinking"])
        eb = dict(kwargs.get("extra_body") or {})
        eb_thinking = dict(eb.get("thinking") or {})
        eb_thinking["type"] = "enabled" if on else "disabled"
        eb["thinking"] = eb_thinking
        kwargs["extra_body"] = eb
        if not on:
            # Belt-and-suspenders: providers that ignore extra_body
            # respect ``reasoning_effort: disable`` to skip thinking.
            kwargs["reasoning_effort"] = "disable"
    if "reasoning_effort" in data:
        # User-set intensity wins over the disable we may have set above
        # (since they're explicitly asking for thinking ON at level X).
        kwargs["reasoning_effort"] = data["reasoning_effort"]
    if "temperature" in data:
        kwargs["temperature"] = data["temperature"]
    if "max_tokens" in data:
        v = data["max_tokens"]
        if v is not None and v > 0:
            kwargs["max_tokens"] = v
        else:
            kwargs.pop("max_tokens", None)


# ---------------------------------------------------------------------------
# Response extraction (resilient to dict / object shapes)
# ---------------------------------------------------------------------------


def _extract_text(resp: Any) -> str:
    try:
        choice = resp.choices[0]
    except Exception:
        return ""
    msg = getattr(choice, "message", None) or (choice.get("message") if isinstance(choice, dict) else None)
    if msg is None:
        return ""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    return content or ""


def _extract_finish_reason(resp: Any) -> str:
    try:
        choice = resp.choices[0]
    except Exception:
        return "unknown"
    reason = getattr(choice, "finish_reason", None)
    if reason is None and isinstance(choice, dict):
        reason = choice.get("finish_reason")
    return reason or "unknown"


def _extract_usage(resp: Any) -> dict | None:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    # pydantic model -> dict
    try:
        return usage.model_dump()
    except Exception:
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
