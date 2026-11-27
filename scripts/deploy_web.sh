#!/usr/bin/env bash
# Deploy the release image, by digest, to one environment's interface (HW3, #94).
#
#   scripts/deploy_web.sh <test|demo> <image@sha256:digest> <commit sha>
#
# Same steps as scripts/deploy_agent.sh: deploy a new revision with no traffic, tagged
# rel-<short sha>; smoke-check its own URL; only then move all traffic to it. The interface
# runs web_main.py from the same image, so the deploy sets that command (OpenTofu creates
# the service with a placeholder image; infra/web.tf).
#
# The smoke check asks for GET /health and the start page, and checks that the page is ours
# and names this release, so a placeholder or a stale revision can't pass it.
#
# Until infra/web.tf is applied the service doesn't exist; this script then says so and
# exits 0, so a release of the other services isn't blocked.
set -euo pipefail

env="${1:?environment: test or demo}"
image="${2:?image reference with digest}"
sha="${3:?full commit sha}"
region="${REGION:-us-central1}"
project="${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
service="course-web-${env}"
tag="rel-${sha:0:7}"

case "${image}" in
  *@sha256:*) ;;
  *) echo "Refusing to deploy ${image}: deploy by digest, not by a tag that can move." >&2; exit 1 ;;
esac

if ! gcloud run services describe "${service}" --project="${project}" --region="${region}" \
     --format='value(metadata.name)' >/dev/null 2>&1; then
  echo "== ${service} does not exist yet (apply infra/web.tf first); skipping the interface deploy"
  exit 0
fi

echo "== Deploying ${image} to ${service} as ${tag}, with no traffic"
gcloud run deploy "${service}" \
  --project="${project}" --region="${region}" \
  --image="${image}" \
  --command=python --args=web_main.py \
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
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "${candidate}/health")"
echo "GET /health -> ${code}"
[ "${code}" = "200" ]

page="$(curl -sf --max-time 60 "${candidate}/")"
grep -q '<h1>Course Autopilot</h1>' <<<"${page}" || { echo "GET / is not the start page" >&2; exit 1; }
grep -q "release <code>${sha:0:7}</code>" <<<"${page}" || { echo "GET / doesn't name release ${sha:0:7}" >&2; exit 1; }
echo "GET / -> the start page, release ${sha:0:7}"

echo "== Smoke check passed; sending all traffic to ${tag}"
gcloud run services update-traffic "${service}" \
  --project="${project}" --region="${region}" \
  --to-tags="${tag}=100" --quiet

echo "Released ${sha} to ${service}: ${image}"
