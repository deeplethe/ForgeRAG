#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# OpenCraig — restore script for the docker compose deploy.
#
# Usage:
#   scripts/restore.sh ./backups/opencraig-20260508T070000Z.tar.gz
#
# Reverses scripts/backup.sh. WARNING: overwrites the live deploy.
# Refuses to run unless the operator types ``restore`` at the prompt.
# ════════════════════════════════════════════════════════════════════════════

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <backup-archive.tar.gz>" >&2
  exit 1
fi
ARCHIVE="$1"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "no such file: $ARCHIVE" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "$HERE")}"
PG_USER="${PG_USER:-opencraig}"
PG_DB="${PG_DB:-opencraig}"
STORAGE_VOLUME="${COMPOSE_PROJECT}_storage"

# ── Confirmation gate ───────────────────────────────────────────────────────
echo "⚠️  About to RESTORE from: $ARCHIVE"
echo "⚠️  This will OVERWRITE the live deploy's data:"
echo "     • postgres database '$PG_DB' (drop + reload)"
echo "     • neo4j database 'neo4j' (load over)"
echo "     • storage volume '$STORAGE_VOLUME' (wipe + extract)"
echo
read -r -p "Type 'restore' to continue: " CONFIRM
[[ "$CONFIRM" == "restore" ]] || { echo "aborted"; exit 1; }

# ── Unpack ──────────────────────────────────────────────────────────────────
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$ARCHIVE" -C "$WORK"
DIR="$(ls "$WORK")"
SRC="$WORK/$DIR"

if [[ ! -f "$SRC/postgres.dump" || ! -f "$SRC/neo4j.dump" || ! -f "$SRC/storage.tar.gz" ]]; then
  echo "archive missing one of: postgres.dump / neo4j.dump / storage.tar.gz" >&2
  exit 1
fi

# ── Stop the application server (let the stores run; we restore them) ───────
echo "==> stopping opencraig"
docker compose stop opencraig

# ── 1. Postgres restore ─────────────────────────────────────────────────────
echo "==> postgres → drop + reload"
docker compose exec -T postgres dropdb -U "$PG_USER" "$PG_DB" --if-exists
docker compose exec -T postgres createdb -U "$PG_USER" "$PG_DB"
docker compose exec -T postgres pg_restore \
  -U "$PG_USER" -d "$PG_DB" \
  --clean --if-exists --no-owner --no-acl \
  < "$SRC/postgres.dump"

# ── 2. Neo4j restore ────────────────────────────────────────────────────────
# ``neo4j-admin database load`` requires the database to be stopped.
echo "==> neo4j → load (briefly stops the db)"
docker compose exec -T neo4j sh -c '
  rm -rf /var/lib/neo4j/dumps && mkdir -p /var/lib/neo4j/dumps
'
docker compose cp "$SRC/neo4j.dump" neo4j:/var/lib/neo4j/dumps/neo4j.dump
# Take the database offline, load, bring it back
docker compose exec -T neo4j sh -c '
  cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "STOP DATABASE neo4j" || true
  neo4j-admin database load neo4j --from-path=/var/lib/neo4j/dumps --overwrite-destination=true
  cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "START DATABASE neo4j"
  rm -f /var/lib/neo4j/dumps/neo4j.dump
'

# ── 3. Storage volume ───────────────────────────────────────────────────────
echo "==> storage volume → wipe + extract"
docker run --rm \
  -v "$STORAGE_VOLUME":/target \
  -v "$SRC":/backup:ro \
  alpine sh -c "rm -rf /target/* /target/.* 2>/dev/null; tar xzf /backup/storage.tar.gz -C /target"

# ── 4. Restart application ──────────────────────────────────────────────────
echo "==> restarting opencraig"
docker compose start opencraig

echo
echo "✅ restore complete"
echo "   verify:  curl http://localhost:8000/api/v1/health"
echo "   audit:   docker compose logs --tail 50 opencraig"
