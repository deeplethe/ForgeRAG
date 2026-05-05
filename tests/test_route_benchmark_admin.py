"""
Benchmark routes are admin-only.

Pre-audit, ``/api/v1/benchmark/*`` endpoints had no authz: any
authenticated user could start a run (consuming LLM quota and
writing per-question doc_ids + ground_truths from the corpus to
``benchmark_results/``), poll status, download the report, or
list previously saved reports.

Audit fix gates every endpoint on ``role=admin``. Auth-disabled
single-user deployments synthesise a local-admin principal so the
gate passes through unchanged.

Cancel + status + reports + report-download all return 403 for
non-admin (not 404 — admin-only endpoints aren't trying to hide
their existence; they're advertising a privilege the caller
lacks).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_state
from api.routes.benchmark import router as benchmark_router
from config.auth_config import AuthConfig


def _build_app(principal: AuthenticatedPrincipal, *, auth_enabled: bool = True) -> FastAPI:
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
    )
    app = FastAPI()
    app.include_router(benchmark_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def _user() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="u_alice", username="alice", role="user", via="session"
    )


def _admin() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id="u_admin", username="admin", role="admin", via="session"
    )


# ---------------------------------------------------------------------------
# Non-admin: every endpoint 403
# ---------------------------------------------------------------------------


class TestBenchmarkNonAdmin:
    def test_start_403(self):
        app = _build_app(_user())
        with TestClient(app) as c:
            r = c.post("/api/v1/benchmark/start", json={"num_questions": 5})
        assert r.status_code == 403

    def test_cancel_403(self):
        app = _build_app(_user())
        with TestClient(app) as c:
            r = c.post("/api/v1/benchmark/cancel")
        assert r.status_code == 403

    def test_status_403(self):
        app = _build_app(_user())
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/status")
        assert r.status_code == 403

    def test_report_403(self):
        app = _build_app(_user())
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/report")
        assert r.status_code == 403

    def test_reports_list_403(self):
        app = _build_app(_user())
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/reports")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Admin & auth-disabled: gate passes through
# ---------------------------------------------------------------------------


class TestBenchmarkAdminPasses:
    def test_admin_status_ok(self):
        """Status doesn't depend on store / runner state for the
        gate test — we just need to confirm the role check passes
        and the route enters its body."""
        app = _build_app(_admin())
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/status")
        # 200 (with whatever the runner returns).
        assert r.status_code == 200

    def test_admin_reports_list_ok(self):
        app = _build_app(_admin())
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/reports")
        assert r.status_code == 200

    def test_auth_disabled_passes(self):
        principal = AuthenticatedPrincipal(
            user_id="local",
            username="local",
            role="admin",
            via="auth_disabled",
        )
        app = _build_app(principal, auth_enabled=False)
        with TestClient(app) as c:
            r = c.get("/api/v1/benchmark/status")
        assert r.status_code == 200
