#!/usr/bin/env bash
# One-shot bootstrap on the remote dev box (10.50.4.54).
#
# Idempotent: rerunning it skips work that's already done.
# Run it ONCE after the first ``dev-sync-remote.ps1`` push:
#
#   ssh yangdongxu@10.50.4.54 "cd HumanIndex && bash scripts/dev-bootstrap-remote.sh"
#
# What it does:
#   1. Creates a Linux venv at ``.venv/`` using the system python3
#      (3.12 on this box; the codebase tests on 3.12 / 3.13 fine).
#   2. Installs the core requirements + the optional packages
#      ``scripts/setup.py``'s resolver picks for our config.
#   3. Writes a remote-flavoured ``.env`` so the backend talks to the
#      LOCAL docker daemon (unix socket) instead of the SSH-tunnelled
#      one we use from Windows. Existing ``.env`` is left untouched.
#   4. Pre-pulls the sandbox image so the first chat doesn't pause
#      ~30s for a transparent docker pull.
#
# Outputs are self-explanatory — green = done, yellow = skipped,
# red = failed. The script ``set -e``s so a real failure stops the
# chain rather than silently leaving half-state.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

c_green=$'\e[32m'
c_yellow=$'\e[33m'
c_dim=$'\e[2m'
c_reset=$'\e[0m'

log()    { printf '%s[bootstrap]%s %s\n'    "$c_green"  "$c_reset" "$*"; }
warn()   { printf '%s[bootstrap]%s %s\n'    "$c_yellow" "$c_reset" "$*"; }
trace()  { printf '%s%s%s\n'                "$c_dim"    "$*"        "$c_reset"; }

# ── 1. venv + deps ────────────────────────────────────────────────
if [[ ! -d .venv ]]; then
    log "creating .venv (Python: $(python3 --version))"
    python3 -m venv .venv
else
    warn ".venv already exists — skipping create"
fi

log "installing core requirements…"
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# Optional deps come from the operator's chosen config. Re-running
# the resolver is cheap (idempotent ``pip install``) and keeps the
# remote in lockstep when we add a new backend to the yaml.
if [[ -f opencraig.yaml ]]; then
    log "syncing optional dependencies from opencraig.yaml…"
    python scripts/setup.py --sync-deps opencraig.yaml || warn "sync-deps failed (non-fatal)"
fi

# ── 2. .env (remote flavour) ──────────────────────────────────────
if [[ ! -f .env ]]; then
    log "writing remote-flavoured .env"
    cat > .env <<'EOF'
# Remote dev box .env — backend runs HERE, daemon is local.
# DOCKER_HOST left UNSET so the SDK uses the default unix socket
# (/var/run/docker.sock) on this machine. With both backend and
# daemon on the same host, bind-mount paths line up natively.

POSTGRES_PASSWORD=ywWrJ8njmE4beJHP
NEO4J_PASSWORD=sheep0916
EOF
else
    warn ".env exists — leaving alone"
fi

# ── 3. pre-pull sandbox image ─────────────────────────────────────
SANDBOX_IMAGE="opencraig/sandbox:py3.13"
if docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
    warn "$SANDBOX_IMAGE already present — skipping pull"
else
    log "pulling $SANDBOX_IMAGE…"
    docker pull "$SANDBOX_IMAGE" || warn "pull failed — run scripts/build-sandbox.sh to build locally"
fi

log "bootstrap complete."
trace "next: edit code on Windows, run 'powershell scripts/dev-sync-remote.ps1 -Restart',"
trace "      tunnel 8000 with 'ssh -L 8000:localhost:8000 yangdongxu@10.50.4.54',"
trace "      browser → http://localhost:8000"
