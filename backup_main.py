"""Entry point: back up this environment's database once, then exit (a Cloud Run job).

    DATABASE_URL=... BACKUP_BUCKET=... BACKUP_PREFIX=demo python backup_main.py

The image runs this for the ``database-backup-<env>`` job (infra/backup.tf); see
``cloud_storage_service.backup`` and docs/database-backup.md. It prints one JSON line and exits
with status 1 if the backup failed, so the job's execution is marked failed.
"""

import sys

from cloud_storage_service.backup import main

if __name__ == "__main__":
    sys.exit(main())
