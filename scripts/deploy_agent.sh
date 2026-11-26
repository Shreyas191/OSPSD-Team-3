#!/usr/bin/env bash
# Deploy the release image, by digest, to one environment's agent service (HW3).
#
#   scripts/deploy_agent.sh <test|demo> <image@sha256:digest> <commit sha>
#
# Same steps as scripts/deploy.sh for the API service: deploy a new revision with no
# traffic, tagged rel-<short sha>; check GET /health on its own URL; only then move all
# traffic to it. The agent service runs agent_main.py from the same image (infra/agent.tf).
#
# OpenTofu creates the agent services, and the deployer may only deploy to services that
# already exist. Until the infra change is applied, this script says so and exits 0, so a
# release of the API service isn't blocked by a service that doesn't exist yet.
set -euo pipefail

env="${1:?environment: test or demo}"
image="${2:?image reference with digest}"
sha="${3:?full commit sha}"
region="${REGION:-us-central1}"
project="${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
service="course-agent-${env}"
tag="rel-${sha:0:7}"

case "${image}" in
  *@sha256:*) ;;
  *) echo "Refusing to deploy ${image}: deploy by digest, not by a tag that can move." >&2; exit 1 ;;
esac

if ! gcloud run services describe "${service}" --project="${project}" --region="${region}" \
     --format='value(metadata.name)' >/dev/null 2>&1; then
  echo "== ${service} does not exist yet (apply infra/agent.tf first); skipping the agent deploy"
  exit 0
fi

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
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "${candidate}/health")"
echo "GET /health -> ${code}"
[ "${code}" = "200" ]

echo "== Smoke check passed; sending all traffic to ${tag}"
gcloud run services update-traffic "${service}" \
  --project="${project}" --region="${region}" \
  --to-tags="${tag}=100" --quiet

echo "Released ${sha} to ${service}: ${image}"
