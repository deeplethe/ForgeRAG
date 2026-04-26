"""Unit tests for the ComponentHealth registry."""

from __future__ import annotations

import time

import pytest

from api.health_registry import HealthRegistry, health_track


def test_record_ok_sets_healthy():
    reg = HealthRegistry()
    reg.record_ok("reranker", latency_ms=123, model="foo")
    snap = reg.snapshot()
    assert snap["reranker"]["status"] == "healthy"
    assert snap["reranker"]["last_latency_ms"] == 123
    assert snap["reranker"]["extra"]["model"] == "foo"
    assert snap["reranker"]["total_ok"] == 1


def test_record_error_sets_error_when_no_prior_ok():
    reg = HealthRegistry()
    reg.record_error("reranker", "BadRequestError", "bad stuff", latency_ms=50)
    snap = reg.snapshot()
    assert snap["reranker"]["status"] == "error"
    assert snap["reranker"]["last_error_type"] == "BadRequestError"
    assert "bad stuff" in snap["reranker"]["last_error_msg"]


def test_record_error_after_recent_ok_sets_degraded():
    """Errors within 5 min of a success downgrade to 'degraded', not full error."""
    reg = HealthRegistry()
    reg.record_ok("reranker", latency_ms=100)
    reg.record_error("reranker", "Timeout", "slow")
    snap = reg.snapshot()
    assert snap["reranker"]["status"] == "degraded"


def test_set_disabled():
    reg = HealthRegistry()
    reg.set_disabled("kg_path")
    snap = reg.snapshot()
    assert snap["kg_path"]["status"] == "disabled"


def test_clear_resets_to_unknown():
    reg = HealthRegistry()
    reg.record_ok("reranker")
    reg.clear("reranker")
    snap = reg.snapshot()
    assert snap["reranker"]["status"] == "unknown"


def test_reset_all_wipes():
    reg = HealthRegistry()
    reg.record_ok("a")
    reg.record_ok("b")
    reg.reset_all()
    assert reg.snapshot() == {}


def test_error_msg_truncated_to_300_chars():
    reg = HealthRegistry()
    reg.record_error("x", "Err", "A" * 1000)
    snap = reg.snapshot()
    assert len(snap["x"]["last_error_msg"]) == 300


def test_health_track_context_manager_ok():
    # Use the real module-level registry via health_track
    from api.health_registry import get_registry

    reg = get_registry()
    reg.clear("ctx_test_ok")
    with health_track("ctx_test_ok") as t:
        t.extra["model"] = "x"
        time.sleep(0.01)
    snap = reg.snapshot()
    assert snap["ctx_test_ok"]["status"] == "healthy"
    assert snap["ctx_test_ok"]["extra"]["model"] == "x"


def test_health_track_context_manager_error():
    from api.health_registry import get_registry

    reg = get_registry()
    reg.clear("ctx_test_err")
    with pytest.raises(ValueError):
        with health_track("ctx_test_err"):
            raise ValueError("boom")
    snap = reg.snapshot()
    assert snap["ctx_test_err"]["status"] == "error"
    assert snap["ctx_test_err"]["last_error_type"] == "ValueError"
    assert "boom" in snap["ctx_test_err"]["last_error_msg"]


def test_singleton_returns_same_instance():
    from api.health_registry import get_registry

    r1 = get_registry()
    r2 = get_registry()
    assert r1 is r2
