# Upgrading OpenCraig

## TL;DR

```bash
./scripts/backup.sh                  # always, no exceptions
docker compose pull                  # fetch the new images
docker compose up -d                 # restart with the new image
docker compose logs -f opencraig     # watch alembic run, look for errors
```

OpenCraig runs `alembic upgrade head` automatically on container
start (controlled by `persistence.relational.schema_auto_init`).
For most upgrades, the four commands above are everything.

## Versioning policy

OpenCraig follows semver-ish:

* **Patch** (0.x.y → 0.x.z): bug fixes only. No schema changes.
  Drop-in upgrade.
* **Minor** (0.x.0 → 0.y.0): new features, possibly additive
  schema migrations (new tables / columns / indexes). Existing
  data is preserved; rolling restart works.
* **Major** (0.x → 1.x, eventually): may include breaking schema
  changes or removed config knobs. Read the release notes first.
  Always run on a staging copy before production.

## Routine upgrade (the common case)

```bash
cd /opt/opencraig                          # your deploy path
./scripts/backup.sh                        # ALWAYS
docker compose pull                        # pull new opencraig image (+ pinned pg/neo4j)
docker compose up -d                       # restart with new
docker compose logs --tail 200 opencraig   # confirm "Application startup complete"
curl http://localhost:8000/api/v1/health   # 200 OK
```

If `alembic upgrade head` runs cleanly, the log will show one or
more `INFO  [alembic.runtime.migration]` lines followed by the
usual uvicorn startup. If it fails, the container exits and the
restart policy retries — fix the underlying issue (see "Recovering
from a failed migration" below) rather than letting it loop.

## Pinning to a specific version

By default `image: opencraig:local` rebuilds from source. For
production, pin to a published tag in `docker-compose.yml`:

```yaml
services:
  opencraig:
    image: ghcr.io/deeplethe/opencraig:v0.5.2   # explicit version
    # build: .                                    # remove build line
```

Then `docker compose pull && docker compose up -d` is a one-shot
upgrade. Skipping the build step also makes CI faster and the
deploy reproducible across hosts.

## Major version upgrades

For 0.x → 1.x style jumps, follow the dedicated runbook (always
linked from the release notes):

1. **Read the release notes.** Breaking changes are called out at
   the top.
2. **Take a backup with `./scripts/backup.sh`.** Keep this
   archive readable until you've validated the upgraded deploy
   for at least 24 hours of normal use — it's your rollback.
3. **Test on staging first.** Restore the backup onto a staging
   instance with the new image, run a synthetic smoke test
   (login, search a known query, verify a known citation, run
   one ingestion job).
4. **Schedule the production upgrade for a low-traffic window.**
   No multi-tenant disruption to worry about (single-tenant
   deploys), but ingestion + KG operations are write-heavy and
   you don't want them to overlap with a migration.
5. **Run.** Backup → pull → up → log-watch.

## Recovering from a failed migration

If alembic fails halfway through and the container exits / loops:

1. **Stop the loop**: `docker compose stop opencraig`. Postgres
   + Neo4j keep running.
2. **Inspect**: `docker compose logs --tail 500 opencraig` —
   the alembic traceback names the migration that failed.
3. **Decision: roll forward or roll back?**
   * Roll forward (preferred when the migration's intent is
     clearly correct but data needed cleanup): connect via
     `docker compose exec postgres psql -U opencraig`, fix the
     data, then `docker compose start opencraig` to retry.
   * Roll back (when the new code is plain wrong): `docker
     compose down`, restore from the pre-upgrade backup with
     `./scripts/restore.sh`, pin the previous image, restart.

## Cross-component upgrades

Upgrading the bundled services (Postgres, Neo4j) follows their
upstream upgrade paths, NOT OpenCraig's release cadence:

* **Postgres minor** (16.x → 16.y): edit `image:` in
  `docker-compose.yml`, `docker compose pull`, restart. Data
  files are forward-compatible.
* **Postgres major** (16 → 17): you must `pg_dump`, drop the
  data volume, start the new image, `pg_restore`. The
  `./scripts/backup.sh` + `./scripts/restore.sh` flow handles
  this, EXCEPT you'll need to manually update `docker-compose.yml`
  to the new image tag between backup and restore.
* **Neo4j minor** (5.20 → 5.24): same as PG minor — image swap +
  restart. Indexes may rebuild on first boot (logged).
* **Neo4j major** (5 → 6): same workflow as PG major; check
  the Neo4j upgrade guide for plugin compatibility (APOC).

## Schema migrations: the "what's actually happening"

OpenCraig uses Alembic for Postgres schema changes. Migrations
live in `persistence/migrations/versions/`. On startup with
`schema_auto_init: true`, the equivalent of:

```bash
alembic upgrade head
```

is run inside the lifespan. A migration that takes longer than
a few seconds will hold up the readiness check; you'll see the
container spend extra time before Postgres traffic flows.

To run migrations manually (e.g. you set `schema_auto_init:
false` in production):

```bash
docker compose exec opencraig alembic upgrade head
docker compose exec opencraig alembic current   # confirm head
```

To roll back ONE migration (rare, mostly for debugging):

```bash
docker compose exec opencraig alembic downgrade -1
```

Don't downgrade past where the application code expects the
schema to be — that's where the rollback-from-backup flow is
safer than a forward / backward dance.

## What to test after an upgrade

A 5-minute smoke checklist that catches 90% of upgrade
regressions:

1. **Health**: `curl /api/v1/health` returns 200.
2. **Login**: existing accounts log in, no 500s.
3. **Workspace**: open a folder, verify documents list.
4. **Search**: run a query you know — verify familiar results
   come back.
5. **Citations**: open a chat, verify a citation hyperlink lands
   at the expected page in the source PDF.
6. **Audit log** (Settings → Activity): the upgrade itself
   shouldn't introduce noise; confirm the most recent entries
   match real user actions.
7. **Ingestion**: upload a small test document, verify it
   completes parsing + KG extraction within the usual window.

If any of these fail, the rollback procedure (above) is your
escape hatch. We'd rather you restore to last-known-good and
file an issue than try to live-debug a bad upgrade.
