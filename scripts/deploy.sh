#!/usr/bin/env bash
# Deploy one image, by digest, to one environment, and give it traffic only if it works.
#
#   scripts/deploy.sh <test|demo> <image@sha256:digest> <commit sha>
#
# 1. Deploy the image as a new Cloud Run revision that receives no traffic, tagged
#    rel-<short sha> so it has its own URL. Users keep reaching the previous revision.
# 2. Smoke-check that URL: GET /health, GET /health/provider (Dropbox accepts our
#    credentials), then the vertical's consumer workflow against real Dropbox, which
#    creates and deletes its own scratch folder.
# 3. Only if every check passed, move 100% of traffic to the new revision.
#
# The image sets RELEASE=<commit> itself (Dockerfile), so the deploy removes any RELEASE a
# revision carried from older releases; otherwise it would override the image's value.
#
# A failed check exits non-zero before step 3, so the pipeline marks the release failed and
# the previous revision keeps serving. Needs gcloud signed in as the deployer and uv on PATH.
set -euo pipefail

env="${1:?environment: test or demo}"
image="${2:?image reference with digest}"
sha="${3:?full commit sha}"
region="${REGION:-us-central1}"
project="${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
service="cloud-storage-service-${env}"
tag="rel-${sha:0:7}"

case "${image}" in
  *@sha256:*) ;;
  *) echo "Refusing to deploy ${image}: deploy by digest, not by a tag that can move." >&2; exit 1 ;;
esac

cd "$(dirname "$0")/.."

echo "== Deploying ${image} to ${service} as ${tag}, with no traffic"
gcloud run deploy "${service}" \
  --project="${project}" --region="${region}" \
  --image="${image}" \
  --no-traffic --tag="${tag}" \
  --update-labels="release=${sha:0:7}" \
  --remove-env-vars=RELEASE \
  --quiet

candidate="$(gcloud run services describe "${service}" --project="${project}" --region="${region}" \
  --format=json | python3 -c '
import json, sys
tag = sys.argv[1]
traffic = json.load(sys.stdin)["status"]["traffic"]
print(next(t["url"] for t in traffic if t.get("tag") == tag))
' "${tag}")"
echo "== Smoke-checking ${candidate}"

check() {
  local path="$1" code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "${candidate}${path}")"
  echo "GET ${path} -> ${code}"
  [ "${code}" = "200" ]
}
check /health
check /health/provider
uv run --no-project --with httpx python scripts/consumer_workflow.py "${candidate}"

echo "== Smoke check passed; sending all traffic to ${tag}"
gcloud run services update-traffic "${service}" \
  --project="${project}" --region="${region}" \
  --to-tags="${tag}=100" --quiet

echo "Released ${sha} to ${env}: ${image}"
