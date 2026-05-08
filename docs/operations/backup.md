# Backup & Restore

OpenCraig stores its state across three independent backends. A
correct backup snapshots all three together; restoring needs to put
them back consistently. This page covers the docker-compose deploy
(recommended for self-host); the same shape applies to bare-metal
deploys with adjusted commands.

## What's in your data

| Backend | What's there | How big? |
|---|---|---|
| **Postgres** | Auth, users, folders, documents, chunks, audit log, conversations, messages | typically a few hundred MB after months of use |
| **Neo4j** | Knowledge graph (entities + relations) | grows with KG-extraction usage; usually <1 GB |
| **Storage volume** | Uploaded files, parser cache, embedding cache, sqlite (if used) | dominated by raw uploads — can be many GB |

Backing up only one of them produces silent corruption on restore
(e.g. documents.path in Postgres pointing at chunks that no longer
exist in chroma / file storage). Always snapshot together.

## Quick start

From the repo root, with the compose stack running:

```bash
./scripts/backup.sh
```

Output: `./backups/opencraig-<UTC-timestamp>.tar.gz`. Inside:

```
opencraig-20260508T070000Z/
├── MANIFEST          # human-readable summary
├── postgres.dump     # pg_dump custom format (compressed)
├── neo4j.dump        # neo4j-admin database dump
└── storage.tar.gz    # the storage volume contents
```

Copy the archive off-host immediately. **A backup that lives on the
same machine as the data is not a backup** — it disappears with the
disk failure that prompted the restore.

## Recommended schedule

| Cadence | Where to keep it | Retention |
|---|---|---|
| Hourly | Same disk (fast restore) | last 24 hours |
| Daily | Off-site (S3 / OSS / NAS) | last 30 days |
| Weekly | Off-site, different region | last 12 months |
| Before every upgrade | Same disk + off-site | until verified post-upgrade |

A simple cron entry that takes the daily backup and rsyncs it
off-host:

```cron
0 3 * * * cd /opt/opencraig && ./scripts/backup.sh && \
  rsync -az ./backups/ user@backup-host:/srv/opencraig-backups/
```

## Restore

```bash
./scripts/restore.sh ./backups/opencraig-20260508T070000Z.tar.gz
```

The script prompts for `restore` confirmation before overwriting
anything. It:

1. Stops the `opencraig` service (the stores keep running so we can
   restore into them).
2. Drops + recreates the Postgres database, then `pg_restore` from
   the dump.
3. Stops the Neo4j database, runs `neo4j-admin database load`,
   restarts it.
4. Wipes the storage volume contents and extracts the tarball.
5. Starts `opencraig` back up.

After the script returns, sanity-check:

```bash
curl http://localhost:8000/api/v1/health     # 200
docker compose logs --tail 50 opencraig      # no startup errors
# Log in, verify a known document opens with citations
```

## Test your backups

A backup you've never restored is a hope, not a backup. Quarterly,
take the last weekly archive and restore it to a staging deploy:

```bash
# On the staging host, with a clean compose stack:
COMPOSE_PROJECT=opencraig-staging \
  ./scripts/restore.sh ./backups/opencraig-<latest>.tar.gz
```

Then run a synthetic check (login as a known user, search for a
known query, verify the citation hits land at the expected page).

## What this is NOT

* **Not point-in-time recovery (PITR).** `pg_dump` is a logical
  backup taken at a single instant; you can't restore "to 14:32:01
  yesterday." If you need PITR, configure `archive_mode=on` +
  `wal_archive_command` on Postgres and stream WAL to off-host.
  Most self-host customers find daily logical backups sufficient.
* **Not online for Neo4j community edition.** The dump pauses
  writes briefly (~5–30s on small graphs); if you need true
  zero-downtime backup for KG, that's a Neo4j Enterprise feature.
* **Not encrypted at rest.** The tarball is plaintext. If your
  threat model requires it, pipe through `gpg --symmetric` or
  store on an encrypted filesystem.

## Cross-version restores

* **Same major version**: always works. Patch-version drift (PG
  16.1 → 16.4, Neo4j 5.20 → 5.24) is fine — the dump formats are
  forward-compatible within a major.
* **Cross major version** (PG 16 → 17, Neo4j 5 → 6 when it ships):
  read the upstream upgrade notes first. `pg_restore` typically
  succeeds; some Neo4j 5→6 changes may require an `apoc` plugin
  bump in compose.
* **Cross OpenCraig version**: the `alembic upgrade head` on
  application start handles schema migration. If the migration
  fails, the restore is salvageable — pin the previous OpenCraig
  image, restart, the data is unchanged.

## Disaster recovery RTO / RPO

For a single-instance compose deploy on commodity hardware:

* **RPO (Recovery Point Objective)** = how much data you can lose:
  whatever your most recent verified off-host backup captured.
  Daily backups → 24 hours of loss in the worst case.
* **RTO (Recovery Time Objective)** = how fast you're back up:
  fresh deploy from scratch + restore = ~15 minutes for small
  deploys (<5 GB), ~1 hour for larger ones bottlenecked on the
  storage tarball.

These numbers are unsuitable for a few classes of customer (high-
frequency-trading research, real-time medical). Those want a
hot standby + streaming replication, which is outside the
single-tenant compose deploy's scope.
