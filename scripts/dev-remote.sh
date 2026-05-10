#!/usr/bin/env bash
# OpenCraig dev workflow — single entrypoint.
#
# Run from Git Bash on Windows (PowerShell's execution policy blocks
# unsigned .ps1 in this user's setup, so we ship the workflow as a
# Bash script that works equally well from Git Bash, WSL, and Linux).
#
# Topology this script supports: code lives on the local Windows
# machine (your IDE / git is here), but the backend runs on the
# remote dev box (10.50.4.54) where the Docker daemon is — that's
# where the agent's per-user sandbox containers can actually
# bind-mount the user-workdirs filesystem. Local Windows + remote
# Docker daemon doesn't work because the daemon can't see Windows
# paths (see prior debug notes; ``host_path=str(user_workdir.resolve())``
# in persistence/sandbox_manager.py is hard-coded local).
#
# Usage:
#   bash scripts/dev-remote.sh [sync | restart | status | logs | tunnel]
#
# Default (no arg) is ``restart`` because that's the loop-iteration
# verb. ``sync`` skips the restart for cases where you want to test
# something on the remote venv without disturbing the running
# backend.

set -euo pipefail

REMOTE_USER='yangdongxu'
REMOTE_HOST='10.50.4.54'
REMOTE_DIR='HumanIndex'                                # under $HOME on remote
SSH_KEY="$HOME/.ssh/id_ed25519"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCREEN_NAME='opencraig'
LOCAL_PORT=8000

c_green=$'\e[32m'; c_yellow=$'\e[33m'; c_cyan=$'\e[36m'
c_red=$'\e[31m'; c_dim=$'\e[2m'; c_reset=$'\e[0m'
log()  { printf '%s[dev]%s %s\n' "$c_cyan"   "$c_reset" "$*"; }
ok()   { printf '%s[dev]%s %s\n' "$c_green"  "$c_reset" "$*"; }
warn() { printf '%s[dev]%s %s\n' "$c_yellow" "$c_reset" "$*"; }
err()  { printf '%s[dev]%s %s\n' "$c_red"    "$c_reset" "$*" >&2; }

remote_ssh() { ssh -o BatchMode=yes "$REMOTE_USER@$REMOTE_HOST" "$@"; }

cmd_sync() {
    log "syncing $LOCAL_ROOT → $REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
    remote_ssh "mkdir -p $REMOTE_DIR" >/dev/null
    # rclone is the chosen transport: rsync isn't on Windows, but
    # rclone is. ``sync`` is rsync-shaped — only changed files cross
    # the wire, deletes propagate after a successful pass.
    rclone sync "$LOCAL_ROOT" ":sftp:$REMOTE_DIR" \
        --sftp-host "$REMOTE_HOST" \
        --sftp-user "$REMOTE_USER" \
        --sftp-key-file "$SSH_KEY" \
        --exclude '.venv/**' \
        --exclude '__pycache__/**' \
        --exclude '*.pyc' \
        --exclude '.pytest_cache/**' \
        --exclude 'node_modules/**' \
        --exclude 'web/dist/**' \
        --exclude 'web/.vite/**' \
        --exclude 'storage/**' \
        --exclude '*.log' \
        --exclude '.smoke-token' \
        --exclude '.env' \
        --exclude '.claude/**' \
        --exclude '.idea/**' \
        --exclude '.vscode/**' \
        --transfers 4 --checkers 8 \
        --stats=0
    ok "sync done"
}

# First-run setup: create venv, install deps, write remote-flavour
# .env, pre-pull sandbox image. Idempotent — rerunning skips work
# that's already done. Triggered automatically by ``cmd_restart``
# the first time it sees no .venv on the remote.
cmd_bootstrap() {
    log "running first-time bootstrap on $REMOTE_HOST…"
    remote_ssh "cd $REMOTE_DIR && bash scripts/dev-bootstrap-remote.sh"
    ok "bootstrap done"
}

cmd_restart() {
    cmd_sync
    # First-run check: if remote has no .venv, run bootstrap before
    # trying to launch python.
    if ! remote_ssh "test -f $REMOTE_DIR/.venv/bin/python" 2>/dev/null; then
        warn "remote .venv missing — bootstrapping first"
        cmd_bootstrap
    fi
    log "restarting remote backend in screen '$SCREEN_NAME'…"
    # Kill any prior ``main.py``, wipe dead screens, relaunch.
    # ``screen -dmS`` detaches; the process keeps running after this
    # ssh closes. Output also tees to ``storage/backend.log`` so
    # ``cmd_logs`` has something to read.
    remote_ssh "cd $REMOTE_DIR && \
        pkill -f 'python.*main\\.py' 2>/dev/null || true; \
        sleep 1; \
        screen -wipe >/dev/null 2>&1 || true; \
        mkdir -p storage; \
        screen -dmS $SCREEN_NAME bash -c 'source .venv/bin/activate && python main.py --config opencraig.yaml --host 127.0.0.1 --port $LOCAL_PORT 2>&1 | tee storage/backend.log'; \
        sleep 2; \
        pgrep -af 'python.*main\\.py' | head -2"
    ok "backend restarted (waiting for boot — try \`bash $0 logs\` or open the tunnel)"
}

cmd_status() {
    log "remote backend status:"
    remote_ssh "pgrep -af 'python.*main\\.py' | head -3 || echo '  (no backend running)'; \
                echo '--- screen sessions ---'; \
                screen -ls 2>/dev/null | grep -i opencraig || echo '  (no screen)'; \
                echo '--- last log lines ---'; \
                tail -n 12 $REMOTE_DIR/storage/backend.log 2>/dev/null || echo '  (no log)'"
}

cmd_logs() {
    # Tail the backend log live until Ctrl-C.
    log "tailing remote backend log (Ctrl-C to stop)…"
    ssh -t "$REMOTE_USER@$REMOTE_HOST" "tail -f $REMOTE_DIR/storage/backend.log"
}

cmd_tunnel() {
    log "opening SSH tunnel localhost:$LOCAL_PORT → $REMOTE_HOST:$LOCAL_PORT (Ctrl-C to stop)"
    log "your browser hits http://localhost:$LOCAL_PORT once this is up"
    # ``-N`` = no remote command (pure tunnel). ``-T`` = no tty (we
    # don't need an interactive shell). ``-o ServerAliveInterval=30``
    # keeps the tunnel from dying behind NATs. The tunnel runs in
    # the foreground so you keep one shell pinned to it; close it
    # by Ctrl-C when you're done.
    ssh -N -T -o ServerAliveInterval=30 \
        -L "$LOCAL_PORT:localhost:$LOCAL_PORT" \
        "$REMOTE_USER@$REMOTE_HOST"
}

case "${1:-restart}" in
    sync)      cmd_sync ;;
    bootstrap) cmd_bootstrap ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    tunnel)    cmd_tunnel ;;
    *)
        err "unknown command: $1"
        echo "usage: bash $0 [sync | bootstrap | restart | status | logs | tunnel]"
        exit 2
        ;;
esac
