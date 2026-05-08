"""
Sandbox image smoke — Phase 2.1.

Cheap structural checks (Dockerfile + requirements.txt + build
scripts) run unconditionally; the actual ``docker run`` import smoke
runs only if the image is already built locally. Building from
scratch takes 10+ min and pulls a few hundred MB — not appropriate
for CI / pre-commit. Developers opt in by running

    scripts/build-sandbox.sh        # one-time
    pytest tests/test_sandbox_image.py

The structural checks catch the most common drift:
  - Dockerfile lost a CLI / pip stack the agent depends on
  - requirements.txt unpinned a critical package
  - build script no longer points at the right context
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "docker" / "sandbox" / "Dockerfile"
REQUIREMENTS = REPO_ROOT / "docker" / "sandbox" / "requirements.txt"
BUILD_SH = REPO_ROOT / "scripts" / "build-sandbox.sh"
BUILD_PS1 = REPO_ROOT / "scripts" / "build-sandbox.ps1"

DEFAULT_IMAGE_TAG = "opencraig/sandbox:py3.13"


# ---------------------------------------------------------------------------
# Structural checks — always run
# ---------------------------------------------------------------------------


def test_dockerfile_exists_and_has_expected_sections():
    assert DOCKERFILE.exists(), f"missing: {DOCKERFILE}"
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Base + non-root + bind-mount + kernel are the design invariants.
    assert "FROM python:3.13-slim" in text, "base image must be python:3.13-slim"
    assert "useradd" in text and "runner" in text, "must create non-root runner user"
    assert "USER runner" in text, "must drop privileges via USER directive"
    assert "WORKDIR /workdir" in text, "workdir mount point missing"
    assert '"tail", "-f", "/dev/null"' in text, (
        "PID 1 must be a no-op keepalive — kernels are launched via "
        "docker exec by the SandboxManager"
    )
    assert "/workspace/.envs" in text, (
        "must pre-create /workspace/.envs/ for Phase 2.8 install_runtime"
    )
    assert "micromamba" in text, "micromamba required for install_runtime"


def test_dockerfile_keeps_the_agent_cli_toolbelt():
    """Each tool in this set is referenced by the roadmap as
    something the agent shells out to. Removing one is a deliberate
    decision, not an accident — this test guards against accidents.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    must_have_apt = [
        "poppler-utils",
        "qpdf",
        "imagemagick",
        "libvips",
        "ffmpeg",
        "tesseract-ocr",
        "tesseract-ocr-chi-sim",
        "jq",
        "sqlite3",
        "ripgrep",
        "fd-find",
        "git",
        "curl",
        "wget",
        "libreoffice",
        "pandoc",
        "texlive-xetex",
    ]
    missing = [t for t in must_have_apt if t not in text]
    assert not missing, f"apt tools missing from Dockerfile: {missing}"

    # Tools NOT in apt — installed via curl
    must_have_curl = ["xsv", "dasel", "duckdb"]
    for tool in must_have_curl:
        assert tool in text, f"{tool} install step missing"


def test_requirements_pins_data_stack():
    """Version pinning catches drift between the image and any
    code (host-side or in-container) that imports these packages.
    Surprise major-version bumps are the kind of silent breakage
    that's hard to debug after-the-fact."""
    assert REQUIREMENTS.exists(), f"missing: {REQUIREMENTS}"
    text = REQUIREMENTS.read_text(encoding="utf-8")
    must_pin = [
        "pandas==",
        "numpy==",
        "matplotlib==",
        "pdfplumber==",
        "openpyxl==",
        "pymupdf==",
    ]
    missing = [p for p in must_pin if p not in text]
    assert not missing, f"requirements pins missing: {missing}"


def test_requirements_does_not_install_kernel_stack():
    """Hermes Agent (the in-container runtime) is subprocess-based;
    we don't run an ipykernel inside the container. Re-introducing
    these would silently revert the model."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    forbidden = ["ipykernel==", "jupyter_client==", "bash_kernel==", "ipywidgets=="]
    present = [f for f in forbidden if f in text]
    assert not present, (
        f"sandbox image must not install kernel stack: {present} — "
        "Hermes Agent uses subprocess execution, not ipykernel"
    )


def test_build_scripts_present_and_executable_or_invocable():
    assert BUILD_SH.exists(), f"missing: {BUILD_SH}"
    assert BUILD_PS1.exists(), f"missing: {BUILD_PS1}"

    sh_text = BUILD_SH.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in sh_text
    assert "docker build" in sh_text
    assert DEFAULT_IMAGE_TAG in sh_text, (
        "build script must default to the same tag the SandboxManager "
        "looks up; drift here is silent (manager would pull from a "
        "registry that doesn't have the image)"
    )

    ps_text = BUILD_PS1.read_text(encoding="utf-8")
    assert DEFAULT_IMAGE_TAG in ps_text


# ---------------------------------------------------------------------------
# Live image smoke — opt-in (requires the image to be already built)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_present(tag: str) -> bool:
    if not _docker_available():
        return False
    res = subprocess.run(
        ["docker", "image", "inspect", tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


@pytest.mark.skipif(
    not _docker_available(), reason="docker CLI not installed"
)
@pytest.mark.skipif(
    not _image_present(DEFAULT_IMAGE_TAG)
    and os.environ.get("OPENCRAIG_REQUIRE_SANDBOX_IMAGE") != "1",
    reason=(
        f"sandbox image {DEFAULT_IMAGE_TAG} not built locally. Run "
        "scripts/build-sandbox.sh first, or set "
        "OPENCRAIG_REQUIRE_SANDBOX_IMAGE=1 to fail loudly."
    ),
)
def test_image_runs_python_data_stack():
    """Live check: the built image can import the pandas stack
    without error. Equivalent to the smoke step at the end of
    build-sandbox.sh; runs ~3s once the image exists."""
    res = subprocess.run(
        [
            "docker", "run", "--rm", DEFAULT_IMAGE_TAG,
            "python", "-c",
            "import pandas, matplotlib, pdfplumber, openpyxl, numpy, "
            "scipy, pymupdf; print('OK')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"stdout={res.stdout!r}  stderr={res.stderr!r}"
    assert "OK" in res.stdout


@pytest.mark.skipif(
    not _docker_available(), reason="docker CLI not installed"
)
@pytest.mark.skipif(
    not _image_present(DEFAULT_IMAGE_TAG),
    reason="sandbox image not built locally",
)
def test_image_has_runner_user_and_micromamba():
    """Sanity: USER directive landed (PID 1 isn't root) and
    micromamba is on PATH (install_runtime needs it)."""
    res = subprocess.run(
        [
            "docker", "run", "--rm", DEFAULT_IMAGE_TAG,
            "bash", "-c", "id -un && id -u && which micromamba && which fd",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip().splitlines()
    assert out[0] == "runner", f"expected runner user, got {out[0]!r}"
    assert out[1] == "1000", f"expected uid 1000, got {out[1]!r}"
    assert "/usr/local/bin/micromamba" in out[2]
    # fd should be on PATH (we symlinked fdfind → fd)
    assert "/usr/local/bin/fd" in out[3] or "fd" in out[3]
