#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# OpenCraig — backup script for the docker compose deploy.
#
#   Output: ./backups/opencraig-<UTC-timestamp>.tar.gz
#
# Captures three things:
#   1. Postgres dump (logical, custom format → restore-friendly across
#      minor version bumps).
#   2. Neo4j store dump (community edition; brief read pause during
#      ``neo4j-admin database dump``).
#   3. The ``storage`` named volume — uploaded blobs, parser cache,
#      embedding cache, sqlite (if used).
#
# Run as a cron weekly, ad-hoc before upgrades, after big imports, etc.
# Tested with the docker-compose stack at the repo root; if you have
# a custom compose project name, point COMPOSE_PROJECT at it.
# ════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Resolve compose context ─────────────────────────────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "$HERE")}"

# Resolve docker volume names. Compose prefixes the project name.
PG_USER="${PG_USER:-opencraig}"
PG_DB="${PG_DB:-opencraig}"
STORAGE_VOLUME="${COMPOSE_PROJECT}_storage"

# ── Output path ─────────────────────────────────────────────────────────────
TS="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="./backups/opencraig-$TS"
ARCHIVE="./backups/opencraig-$TS.tar.gz"

mkdir -p "$WORK"
echo "==> writing backup to $ARCHIVE"

# ── 1. Postgres dump ────────────────────────────────────────────────────────
# pg_dump -F c is custom format: compressed, parallel-restorable,
# tolerant of schema-version drift on restore.
echo "==> postgres → $WORK/postgres.dump"
docker compose exec -T postgres pg_dump \
  -U "$PG_USER" -d "$PG_DB" \
  --format=custom --compress=9 \
  > "$WORK/postgres.dump"

# ── 2. Neo4j dump ───────────────────────────────────────────────────────────
# Community edition: ``neo4j-admin database dump`` works while the
# database is offline OR online for the system database; we use the
# online path here. Briefly pauses writes (~5s on small graphs).
echo "==> neo4j → $WORK/neo4j.dump"
docker compose exec -T neo4j sh -c '
  rm -rf /var/lib/neo4j/dumps && mkdir -p /var/lib/neo4j/dumps
  neo4j-admin database dump neo4j --to-path=/var/lib/neo4j/dumps --overwrite-destination=true
'
docker compose cp neo4j:/var/lib/neo4j/dumps/neo4j.dump "$WORK/neo4j.dump"
docker compose exec -T neo4j rm -f /var/lib/neo4j/dumps/neo4j.dump

# ── 3. Storage volume ───────────────────────────────────────────────────────
# Run a throwaway alpine container with the named volume mounted ro
# and tar everything to a streamed gzip. Faster than docker cp.
echo "==> storage volume ($STORAGE_VOLUME) → $WORK/storage.tar.gz"
docker run --rm \
  -v "$STORAGE_VOLUME":/source:ro \
  -v "$(pwd)/$WORK":/backup \
  alpine \
  tar czf /backup/storage.tar.gz -C /source .

# ── 4. Manifest ─────────────────────────────────────────────────────────────
cat > "$WORK/MANIFEST" <<EOF
opencraig backup
timestamp:    $TS
hostname:     $(hostname)
compose_proj: $COMPOSE_PROJECT
postgres_dump: $(du -h "$WORK/postgres.dump" | cut -f1)
neo4j_dump:    $(du -h "$WORK/neo4j.dump" | cut -f1)
storage:       $(du -h "$WORK/storage.tar.gz" | cut -f1)
EOF

# ── 5. Bundle ───────────────────────────────────────────────────────────────
echo "==> packaging…"
tar -czf "$ARCHIVE" -C ./backups "opencraig-$TS"
rm -rf "$WORK"

echo
echo "✅ backup complete"
echo "   archive: $ARCHIVE"
echo "   size:    $(du -h "$ARCHIVE" | cut -f1)"
echo
echo "Next steps:"
echo "  • Copy $ARCHIVE off-site (S3 / OSS / a different host)"
echo "  • Verify restore quarterly on a staging copy: scripts/restore.sh $ARCHIVE"
