#!/usr/bin/env bash
# Build the service image and tag it with the full SHA of the commit it was built from.
#
#   scripts/build_image.sh [image-name]      # default name: cloud-storage-service
#
# It refuses to build from a working tree with uncommitted or untracked changes, because
# the image would then not match the commit in its tag. It builds for linux/amd64, the
# platform Cloud Run runs; set PLATFORM to override (for example linux/arm64 for a quick
# local run on Apple silicon). It never tags `latest`.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "Refusing to build: commit or stash your changes first, so the image matches its tag." >&2
  exit 1
fi

sha="$(git rev-parse HEAD)"
image="${1:-cloud-storage-service}"
platform="${PLATFORM:-linux/amd64}"

docker build \
  --platform "${platform}" \
  --build-arg "RELEASE=${sha}" \
  --tag "${image}:${sha}" \
  .

echo "${image}:${sha}"
