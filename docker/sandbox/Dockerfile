# OpenCraig agent sandbox — Phase 2 base image.
#
# This image is what the per-user agent containers run from. The
# SandboxManager (Phase 2.2) does:
#
#   docker run -d --name <user>-sandbox \
#       -v <host>/storage/projects/<pid>:/workdir/<pid> \
#       -v <host>/storage/user-envs/<uid>:/workspace/.envs \
#       opencraig/sandbox:py3.13
#
# Container's PID 1 just keeps the box alive; the in-container
# agent runtime (Claude Agent SDK, Anthropic, MIT) is launched via
# ``docker exec`` per chat turn and reaches back to the backend's
# MCP server + LLM proxy over loopback. The container never opens
# a public port — outbound HTTP only, no inbound listeners.
#
# Design notes:
# - Non-root ``runner`` user (uid 1000) inside. With Linux daemon-
#   level userns-remap (Phase 2.9 docs), runner-as-root maps to an
#   unprivileged uid on the host. Without remap (Windows DD / macOS
#   DD), runner is just a non-root user inside — still better than
#   running as root for defense-in-depth.
# - micromamba pre-installed at /opt/conda so Phase 2.8's
#   install_runtime("r"/"julia"/"node"/...) can lazy-install
#   non-Python toolchains into /workspace/.envs/<lang>/ without
#   needing apt root.
# - Python data stack via pip (faster + more deterministic than
#   conda for the base; conda kicks in only for non-Python lazy
#   installs).
# - ~25 CLI tools chosen for "agent shells out to do something a
#   library doesn't" — pdf / image / audio / OCR / data / search.
# - Image size target: ~1.5 GB. LibreOffice + texlive-xetex push
#   us up but they earn the slot ("write a markdown report and
#   convert to docx/pdf" needs them).

FROM python:3.13-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    # Privacy: opt out of every dependency's anonymous telemetry up
    # front, same as we do at api.state import time.
    LITELLM_TELEMETRY=False \
    ANONYMIZED_TELEMETRY=False \
    DO_NOT_TRACK=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# ── OS-level CLI toolbelt ─────────────────────────────────────────
# Each cluster justifies its own apt invocation in comments so a
# future trim pass can drop a cluster cleanly. Install + clean in
# one RUN to keep layer size down.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       # PDF: extract text / images / metadata; manipulate pages
       poppler-utils qpdf \
       # Image: convert / resize / EXIF / format detection
       imagemagick libvips libvips-tools \
       # Audio + video: trim / transcode / extract audio for STT
       ffmpeg \
       # OCR: scanned PDFs / images. Includes Simplified Chinese.
       tesseract-ocr tesseract-ocr-chi-sim \
       # Data CLIs: jq for JSON, sqlite3 client for ad-hoc queries
       jq sqlite3 \
       # Search / files: grep replacement (rg = ripgrep), find
       # replacement (fd-find binary is `fdfind` on Debian)
       ripgrep fd-find \
       # Network / VCS: agent uses these via subprocess
       git curl wget ca-certificates \
       # Office: docx / pptx / xlsx → pdf / markdown via libreoffice
       libreoffice \
       # Universal doc converter: markdown ↔ docx ↔ html ↔ tex
       pandoc \
       # PDF rendering for pandoc → pdf path; xetex for unicode
       texlive-xetex \
       # Build deps for any pip wheel that doesn't ship a manylinux
       build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Extra CLIs not in apt ─────────────────────────────────────────
# xsv (csv toolkit), dasel (jq for yaml/toml/xml), duckdb CLI.
# Pinned versions — these are agent-facing tools, surprise upgrades
# would invalidate `bash_exec` recipes the LLM has memorised.
RUN set -eux; \
    arch=$(dpkg --print-architecture); \
    case "$arch" in \
        amd64) xsv_url=https://github.com/BurntSushi/xsv/releases/download/0.13.0/xsv-0.13.0-x86_64-unknown-linux-musl.tar.gz; \
               dasel_url=https://github.com/TomWright/dasel/releases/download/v2.8.1/dasel_linux_amd64; \
               duckdb_url=https://github.com/duckdb/duckdb/releases/download/v1.1.3/duckdb_cli-linux-amd64.zip ;; \
        arm64) xsv_url=https://github.com/BurntSushi/xsv/releases/download/0.13.0/xsv-0.13.0-aarch64-unknown-linux-musl.tar.gz; \
               dasel_url=https://github.com/TomWright/dasel/releases/download/v2.8.1/dasel_linux_arm64; \
               duckdb_url=https://github.com/duckdb/duckdb/releases/download/v1.1.3/duckdb_cli-linux-aarch64.zip ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    # xsv
    curl -fsSL "$xsv_url" | tar -xz -C /usr/local/bin xsv; \
    chmod +x /usr/local/bin/xsv; \
    # dasel
    curl -fsSL -o /usr/local/bin/dasel "$dasel_url"; \
    chmod +x /usr/local/bin/dasel; \
    # duckdb
    apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/duckdb.zip "$duckdb_url"; \
    unzip /tmp/duckdb.zip -d /usr/local/bin && rm /tmp/duckdb.zip; \
    chmod +x /usr/local/bin/duckdb

# Symlink fdfind → fd so the agent can call ``fd`` like everyone
# else's docs say (Debian renames it to avoid clashing with a
# legacy package).
RUN ln -sf "$(command -v fdfind)" /usr/local/bin/fd

# ── micromamba (for Phase 2.8 install_runtime) ────────────────────
# Single-binary conda replacement; doesn't need a base env to
# bootstrap. We install it system-wide so install_runtime can use it
# from inside any kernel without messing with PATH per-user.
ENV MAMBA_ROOT_PREFIX=/opt/conda
RUN set -eux; \
    arch=$(dpkg --print-architecture); \
    case "$arch" in \
        amd64) mm_url=https://micro.mamba.pm/api/micromamba/linux-64/latest ;; \
        arm64) mm_url=https://micro.mamba.pm/api/micromamba/linux-aarch64/latest ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "$mm_url" | tar -xj -C /usr/local bin/micromamba; \
    /usr/local/bin/micromamba --version; \
    mkdir -p ${MAMBA_ROOT_PREFIX}

ENV PATH="/opt/conda/bin:${PATH}"

# ── Python data stack ─────────────────────────────────────────────
# Pinned via requirements.txt next to the Dockerfile so a `pip
# install -r` smoke test from CI catches version drift before the
# rebuild lands. Wheels are pulled from PyPI; no compile time
# expected on amd64 manylinux. arm64 may compile a few (numpy /
# pandas have wheels; the "could compile" risk sits on
# camelot-py / pymupdf / pdf2image which sometimes lag).
COPY requirements.txt /tmp/sandbox-requirements.txt
RUN pip install --no-cache-dir -r /tmp/sandbox-requirements.txt \
    && rm /tmp/sandbox-requirements.txt

# ── Claude Agent SDK CLI on PATH ─────────────────────────────────
# The wheel installs a per-platform self-contained binary at
# ``<site-packages>/claude_agent_sdk/_bundled/claude``. The SDK's
# ``_find_cli()`` already checks that path first, but symlinking it
# to /usr/local/bin/claude lets bash recipes / docker exec /
# debugging shells invoke it directly without going through the
# Python wrapper. Idempotent — symlink only if the source exists.
RUN set -eux; \
    src=$(python -c "import claude_agent_sdk, os; \
print(os.path.join(os.path.dirname(claude_agent_sdk.__file__), '_bundled', 'claude'))"); \
    if [ -f "$src" ]; then \
        chmod +x "$src"; \
        ln -sf "$src" /usr/local/bin/claude; \
    else \
        echo "WARNING: claude binary not found at $src" >&2; \
    fi

# ── OpenCraig agent-turn entrypoint ─────────────────────────────
# This script is what the backend's HermesContainerRunner invokes
# via ``docker exec``. Lives at a stable path so the backend can
# call it without consulting docker-image labels.
COPY opencraig_run_turn.py /opt/opencraig/opencraig_run_turn.py

# ── Non-root user ────────────────────────────────────────────────
# uid 1000 to match the typical first user on the host (matters for
# bind-mount ownership when userns-remap is OFF). Group 1000 too.
# When userns-remap is ON (Linux daemon config), runner→uid 1000
# inside maps to host uid 100000+1000 = 101000.
RUN groupadd -g 1000 runner \
    && useradd -m -u 1000 -g 1000 -s /bin/bash runner

# Mount points get pre-created with runner ownership so bind-mounts
# inherit a sane mode if the host dir is fresh.
RUN mkdir -p /workdir /workspace/.envs \
    && chown -R runner:runner /workdir /workspace ${MAMBA_ROOT_PREFIX}

USER runner
WORKDIR /workdir

# Container's PID 1 is just a keeper-alive — the SandboxManager
# launches kernels via ``docker exec``. ``tail -f /dev/null`` is the
# canonical "do nothing forever" recipe; safer than `sleep infinity`
# on busybox-style /bin/sleep.
CMD ["tail", "-f", "/dev/null"]
