# Database Backup

A daily logical backup of the Course Autopilot database, for launch week (Dec 2–6). Neon's free plan can restore a branch only within the **last 6 hours**, so damage noticed the next morning couldn't otherwise be undone. A daily `pg_dump` closes that gap: we can go back to the start of any of the last 14 days.

## What runs

| Part | What it is | Defined in |
|---|---|---|
| Job `database-backup-<env>` | A Cloud Run job running `backup_main.py` from the release image: `pg_dump --format=custom` of the environment's database, uploaded as `gs://<project>-database-backups/<env>/<UTC time>.dump` | `infra/backup.tf`, `cloud_storage_service/backup.py` |
| Schedule | Cloud Scheduler starts the job daily at **04:00 Eastern** (`database_backup_schedule`) | `infra/backup.tf` |
| Bucket `<project>-database-backups` | Private (public access prevented, uniform access). Each dump is **deleted after 14 days** (`database_backup_retention_days`), with no soft-delete copy | `infra/backup.tf` |
| Image updates | Each release points the job at its image, after the API service is deployed | `scripts/deploy_backup_job.sh`, the `deploy` job |

**Permissions.** The job's identity (`db-backup-<env>`) can read only its environment's `database-url-<env>` and **create** objects in the bucket: it can't read, replace or delete a backup, so a broken or compromised job can't destroy the earlier ones. An upload never replaces an existing object (`ifGenerationMatch=0`). The scheduler's identity can only start the job. Reading a dump needs project owner access.

**What a dump holds.** Every table: users, sessions (token hashes only), conversations and messages, proposals and their outcomes, and provider connections with their tokens **still encrypted**. Restoring connections needs the same `token-encryption-key-<env>`, so that key isn't rotated during launch week. The connection string never appears in a log line: `pg_dump`'s own error text can name the host and user, so only its exit status is kept.

**PostgreSQL version.** `pg_dump` refuses a server newer than itself. Neon runs **PostgreSQL 18** (18.7, read from a test dump on Nov 27), and Debian trixie, the image's base, ships client 17, so the image installs `postgresql-client-18` from the PostgreSQL project's repository (its signing key is `docker/apt.postgresql.org.asc`, fingerprint `B97B 0AFC AA1A 47F0 44F2 44A0 7FCC 7D46 ACCC 4CF8`). If Neon moves to a newer major version, the job fails with "pg_dump 18.x is older than the database server …"; change the package to match.

**Neon's pooler.** `DATABASE_URL` is Neon's pooled connection string (host `ep-…-pooler…`), which goes through PgBouncer. `pg_dump` needs a direct connection, so the job drops `-pooler` from the host and connects to the same endpoint directly.

## Turning it on

1. Merge this change, and let a release that includes `backup_main.py` deploy to the environment.
2. Add the environment to `infra/terraform.tfvars` and apply:

    ```hcl
    database_backup_environments = ["demo"]
    ```

    OpenTofu creates the bucket, the job (on the image the API service is serving), the schedule and their permissions, and enables the Cloud Storage and Cloud Scheduler APIs.

3. Run it once by hand and check the result (next section).

## Running it by hand, and checking it

```bash
gcloud run jobs execute database-backup-demo --region=us-central1 --wait
gcloud storage ls -l gs://ospsd-team3-database-backups/demo/
```

Each run writes one log line, `{"event": "database_backup", "outcome": "ok", "object": ..., "bytes": ..., "duration_ms": ...}`, or `"outcome": "failed"` with a reason and the execution marked failed:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND jsonPayload.event="database_backup"' \
  --freshness=2d --format="table(timestamp,jsonPayload.outcome,jsonPayload.object,jsonPayload.bytes,jsonPayload.reason)"
```

The on-duty person checks this each morning with the reconciliation queries ([rollout plan](https://github.com/Shreyas191/OSPSD-Team-3/pull/120), section 8). A `pg_dump failed with exit status 1` most often means the database was unreachable at 04:00; run it again by hand.

## Restoring

Restore into a **throwaway Neon branch** first, never straight over production, then decide what to copy back.

```bash
# 1. Download the dump to restore (project owner access).
gcloud storage cp gs://ospsd-team3-database-backups/demo/2026-12-03T090000Z.dump ./restore.dump
# 2. Create an empty branch in the Neon console (or `neon branches create --name restore-check`),
#    and copy its direct (unpooled) connection string into RESTORE_URL from a hidden prompt.
read -rs RESTORE_URL
# 3. Restore into it.
pg_restore --no-owner --no-privileges --clean --if-exists --dbname="$RESTORE_URL" ./restore.dump
unset RESTORE_URL
```

Compare row counts with production (`users`, `proposals`, `change_outcomes`), then either copy back what was lost or point the service at the restored branch by updating `database-url-<env>`. Delete the downloaded file and the throwaway branch afterwards; the dump holds students' conversations.

## Turning it off, and teardown

Remove the environment from `database_backup_environments` and apply: the job and schedule go. The bucket stays while it holds dumps (`force_destroy = false`), so `tofu destroy` can't delete backups unnoticed. To remove it at the end of the course, empty it first:

```bash
gcloud storage rm --recursive gs://ospsd-team3-database-backups/**
```
