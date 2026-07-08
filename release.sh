#!/usr/bin/env bash
# Manual release helper — tag + push an ALREADY-BUILT local vibeic-eda image to
# Docker Hub + GHCR. Use this for the first release (the image is already built
# locally) without waiting on the multi-hour CI from-source rebuild.
#
#   ./release.sh 0.2.5
#
# Prereqs (one-time):
#   docker login                                   # Docker Hub, user in the `vibeic` org
#   echo "$GHCR_PAT" | docker login ghcr.io -u <user> --password-stdin   # PAT with write:packages
set -euo pipefail

VER="${1:?usage: ./release.sh <version>  e.g. ./release.sh 0.2.5}"
SRC="${SRC_IMAGE:-vibeic/vibeic-eda:${VER}}"     # local source tag (override with SRC_IMAGE=)

echo "==> source image: ${SRC}"
docker image inspect "${SRC}" >/dev/null 2>&1 || {
  echo "ERROR: local image ${SRC} not found. Build it first (docker build -t ${SRC} .) or set SRC_IMAGE=." >&2
  exit 1
}

TAGS=(
  "vibeic/vibeic-eda:${VER}"
  "vibeic/vibeic-eda:latest"
  "ghcr.io/vibeic/vibeic-eda:${VER}"
  "ghcr.io/vibeic/vibeic-eda:latest"
)

for t in "${TAGS[@]}"; do
  echo "==> tag ${t}"
  docker tag "${SRC}" "${t}"
done

for t in "${TAGS[@]}"; do
  echo "==> push ${t}"
  docker push "${t}"
done

echo
echo "DONE. Released:"
printf '  %s\n' "${TAGS[@]}"
echo
echo "Verify:  docker pull vibeic/vibeic-eda:${VER} && docker run --rm vibeic/vibeic-eda:${VER} yosys --version"
