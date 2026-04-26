"""
Component health registry.

Tracks the last-known operational status of each pipeline component
(reranker, embedder, KG path, tree path, query understanding, answer
generator, etc). Components call `record_ok()` / `record_error()` after
each real invocation, and the /api/v1/health/components endpoint
exposes the aggregate state to the Web UI so failures surface as red
dots on the architecture graph without requiring users to dig through
logs.

Design goals:
    - Thread-safe (components run on many request threads)
    - Zero-dep (stdlib only)
    - Cheap: no I/O in hot path, just in-memory counters
    - Optional periodic probe() hook: startup + N-minute retest

Four possible states per component:
    - "healthy"  — last call succeeded, or healthy probe()
    - "degraded" — some recent errors but not every call fails
    - "error"    — last call failed; last_error populated
    - "disabled" — feature explicitly turned off in config
    - "unknown"  — never been called yet (fresh start)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComponentHealth:
    """Mutable snapshot of one component's operational history."""

    component: str  # e.g. "reranker"
    status: str = "unknown"  # healthy | degraded | error | disabled | unknown
    last_ok_ts: float | None = None  # epoch seconds
    last_error_ts: float | None = None
    last_error_type: str | None = None  # e.g. "BadRequestError"
    last_error_msg: str | None = None  # short message, trimmed to 300 chars
    last_latency_ms: int | None = None  # latency of the most recent call
    total_ok: int = 0
    total_err: int = 0
    extra: dict[str, Any] = field(default_factory=dict)  # component-specific metadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status,
            "last_ok_ts": self.last_ok_ts,
            "last_error_ts": self.last_error_ts,
            "last_error_type": self.last_error_type,
            "last_error_msg": self.last_error_msg,
            "last_latency_ms": self.last_latency_ms,
            "total_ok": self.total_ok,
            "total_err": self.total_err,
            "extra": self.extra,
        }


class HealthRegistry:
    """
    Thread-safe in-memory registry. One instance per process (see
    get_registry()). Multi-worker deployments get one registry per
    worker — callers query via the API and will see whichever worker
    handled the request. That's acceptable: failure patterns tend to
    be config-driven and correlate across workers.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._components: dict[str, ComponentHealth] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def _get_or_create(self, name: str) -> ComponentHealth:
        c = self._components.get(name)
        if c is None:
            c = ComponentHealth(component=name)
            self._components[name] = c
        return c

    def record_ok(self, name: str, latency_ms: int | None = None, **extra: Any) -> None:
        """Mark a successful call. Transitions to 'healthy'."""
        with self._lock:
            c = self._get_or_create(name)
            c.last_ok_ts = time.time()
            c.last_latency_ms = latency_ms
            c.total_ok += 1
            c.status = "healthy"
            if extra:
                c.extra.update(extra)

    def record_error(
        self,
        name: str,
        error_type: str,
        error_msg: str,
        latency_ms: int | None = None,
    ) -> None:
        """Mark a failed call. Sets status to 'error' (or 'degraded' if recent ok)."""
        with self._lock:
            c = self._get_or_create(name)
            c.last_error_ts = time.time()
            c.last_error_type = error_type
            # Trim to avoid bloating /health/components payload with stack traces
            c.last_error_msg = (error_msg or "")[:300]
            c.last_latency_ms = latency_ms
            c.total_err += 1
            # If we had a recent success (within 5 minutes), mark 'degraded'
            # rather than hard 'error' — transient errors shouldn't hide history.
            if c.last_ok_ts is not None and time.time() - c.last_ok_ts < 300:
                c.status = "degraded"
            else:
                c.status = "error"

    def set_disabled(self, name: str) -> None:
        """Mark a component as intentionally disabled (feature toggle off)."""
        with self._lock:
            c = self._get_or_create(name)
            c.status = "disabled"

    def clear(self, name: str) -> None:
        """Reset a component back to 'unknown' (used on config reload)."""
        with self._lock:
            if name in self._components:
                self._components[name] = ComponentHealth(component=name)

    def reset_all(self) -> None:
        """Wipe the registry — used on server restart."""
        with self._lock:
            self._components.clear()

    # ------------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a deep-ish copy safe to serialize as JSON."""
        with self._lock:
            return {name: c.to_dict() for name, c in self._components.items()}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_registry: HealthRegistry | None = None
_singleton_lock = threading.Lock()


def get_registry() -> HealthRegistry:
    global _registry
    if _registry is None:
        with _singleton_lock:
            if _registry is None:
                _registry = HealthRegistry()
    return _registry


# ---------------------------------------------------------------------------
# Convenience decorator for components to instrument their calls
# ---------------------------------------------------------------------------


class health_track:
    """
    Context-manager that times a block of code and records the outcome.

    Usage:
        with health_track("reranker") as t:
            resp = litellm.rerank(...)
            # automatic record_ok on __exit__; record_error on exception
            t.extra["model"] = model_name
    """

    def __init__(self, name: str):
        self.name = name
        self.start_ts = 0.0
        self.extra: dict[str, Any] = {}

    def __enter__(self) -> health_track:
        self.start_ts = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        latency_ms = int((time.time() - self.start_ts) * 1000)
        reg = get_registry()
        if exc_val is None:
            reg.record_ok(self.name, latency_ms=latency_ms, **self.extra)
        else:
            reg.record_error(
                self.name,
                error_type=type(exc_val).__name__,
                error_msg=str(exc_val),
                latency_ms=latency_ms,
            )
        return False  # don't swallow
