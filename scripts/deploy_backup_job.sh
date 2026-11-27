#!/usr/bin/env bash
# Point one environment's database backup job at the release image (docs/database-backup.md).
#
#   scripts/deploy_backup_job.sh <test|demo> <image@sha256:digest>
#
# The job runs backup_main.py from the same image as the services, so each release updates
# it after the API service is deployed. This changes the job only; it doesn't run it (Cloud
# Scheduler does, daily). Environments without a job (not listed in
# database_backup_environments, infra/backup.tf) are skipped.
set -euo pipefail

env="${1:?environment: test or demo}"
image="${2:?image reference with digest}"
region="${REGION:-us-central1}"
project="${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
job="database-backup-${env}"

case "${image}" in
  *@sha256:*) ;;
  *) echo "Refusing to use ${image}: deploy by digest, not by a tag that can move." >&2; exit 1 ;;
esac

if ! gcloud run jobs describe "${job}" --project="${project}" --region="${region}" \
     --format='value(metadata.name)' >/dev/null 2>&1; then
  echo "== ${job} does not exist (no daily backup in ${env}); skipping"
  exit 0
fi

echo "== Pointing ${job} at ${image}"
gcloud run jobs update "${job}" \
  --project="${project}" --region="${region}" \
  --image="${image}" \
  --quiet
echo "Updated ${job}"
