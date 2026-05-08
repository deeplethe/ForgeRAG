"""
Regression test: OpenCraig does not import known telemetry SDKs and
does set the documented opt-out env vars before any heavy import.

PRIVACY.md commits us to "no telemetry, no phone home, no error
reporting back to OpenCraig itself". This test enforces that
commitment in CI — a PR that introduces ``import sentry_sdk``
fails here, before review is even possible.

Pair with the manual audit grep in PRIVACY.md.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Source dirs we own + ship. Excluded: tests (which legitimately
# import / mock); .venv; node_modules; web/dist (build output).
PYTHON_DIRS = [
    "api",
    "ingestion",
    "persistence",
    "config",
    "embedder",
    "parser",
    "retrieval",
    "graph",
]
FRONTEND_DIR = "web/src"

# SDKs that report data home. Free to add to this list as new ones
# emerge; the test only checks that none of them are imported.
FORBIDDEN_SDKS = [
    "sentry_sdk",
    "sentry-sdk",
    "posthog",
    "mixpanel",
    "amplitude",
    "segment_analytics",
    "datadog",          # both datadog and ddtrace
    "ddtrace",
    "newrelic",
    "rollbar",
    "bugsnag",
    "honeybadger",
    "raygun",
]


def _iter_source_files() -> list[Path]:
    """Walk the source dirs collecting reviewable files. Skips
    common build / cache / venv detritus that grep would also
    have ignored."""
    skip_dirs = {"__pycache__", "node_modules", ".venv", "dist", "build"}
    out: list[Path] = []
    for d in PYTHON_DIRS:
        root = REPO_ROOT / d
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if any(seg in skip_dirs for seg in p.parts):
                continue
            out.append(p)
    front = REPO_ROOT / FRONTEND_DIR
    if front.exists():
        for ext in ("*.js", "*.ts", "*.vue"):
            for p in front.rglob(ext):
                if any(seg in skip_dirs for seg in p.parts):
                    continue
                out.append(p)
    return out


def _is_comment_line(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("#", "//", "*", "/*"))


def test_no_telemetry_sdk_imports():
    """No source file imports a known telemetry SDK.

    Match with word boundaries (or hyphen-aware boundaries) so
    common substrings don't false-positive — e.g. ``rollbar``
    inside ``scrollbar``, ``datadog`` inside ``datadoghq.com`` is
    fine, but ``import datadog`` is not.
    """
    # Word-boundary based — names made of \w (letters/digits/_) get
    # \b on both ends; names containing ``-`` get a custom boundary
    # since ``-`` isn't a word char in regex \b.
    parts = []
    for s in FORBIDDEN_SDKS:
        if "-" in s:
            parts.append(rf"(?<![\w\-]){re.escape(s)}(?![\w\-])")
        else:
            parts.append(rf"\b{re.escape(s)}\b")
    pat = re.compile("|".join(parts))
    hits: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for ln_no, line in enumerate(text.splitlines(), 1):
            if not pat.search(line):
                continue
            if _is_comment_line(line):
                continue
            hits.append(f"{path.relative_to(REPO_ROOT)}:{ln_no}: {line.strip()}")
    assert not hits, (
        "telemetry SDK import detected — PRIVACY.md commits us to "
        "shipping zero phone-home dependencies. If a real need has "
        "appeared, document it in PRIVACY.md and update FORBIDDEN_SDKS "
        "here. Hits:\n  " + "\n  ".join(hits)
    )


def test_state_module_disables_dep_telemetry():
    """``api/state.py`` sets the documented opt-out env vars early.

    Catches accidental removal of the privacy hardening block during
    a refactor — those env vars take effect ONLY before the relevant
    package is imported, so deleting the block silently re-enables
    bundled-dep telemetry on next deploy.
    """
    src = (REPO_ROOT / "api" / "state.py").read_text(encoding="utf-8")
    expected = [
        ("LITELLM_TELEMETRY", "False"),
        ("ANONYMIZED_TELEMETRY", "False"),
        ("DO_NOT_TRACK", "1"),
        ("HF_HUB_DISABLE_TELEMETRY", "1"),
    ]
    for name, expected_value in expected:
        m = re.search(
            rf'os\.environ\.setdefault\(\s*"{re.escape(name)}"\s*,\s*"([^"]+)"\s*\)',
            src,
        )
        assert m is not None, (
            f"api/state.py no longer contains an os.environ.setdefault "
            f'call for {name!r}. PRIVACY.md commits us to disabling '
            f"bundled-dep telemetry; restore the call (or update both "
            f"this test and PRIVACY.md if the env var name changed)."
        )
        assert m.group(1) == expected_value, (
            f"{name} is set to {m.group(1)!r}, expected {expected_value!r} — "
            "an opt-IN value was committed instead of an opt-out."
        )


def test_privacy_md_present():
    """PRIVACY.md exists at the repo root and references the
    opt-out file path. If someone moves api/state.py without
    updating the doc, this catches it."""
    privacy = REPO_ROOT / "PRIVACY.md"
    assert privacy.exists(), "PRIVACY.md is missing from repo root"
    content = privacy.read_text(encoding="utf-8")
    assert "api/state.py" in content, (
        "PRIVACY.md no longer references api/state.py — keep the "
        "audit pointer accurate so customers can verify the claim."
    )
