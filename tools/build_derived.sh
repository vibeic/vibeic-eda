#!/usr/bin/env bash
# Build the 0.3.47 derived image from the 0.3.46 digest.
#
# This exists because the release composition cannot be built on the hosts that
# need the image: `docker buildx` is absent and no credential here has
# read:packages for the private eda-tool-* artefacts. Owner ruling 2026-09-07.
#
# It stages a build context rather than using the repo as one, because the
# ~145 MB openroad binary must not be committed to this repository.
#
# Usage:
#   tools/build_derived.sh <path-to-openroad-binary> [tag]
set -euo pipefail

OPENROAD_BIN="${1:?usage: build_derived.sh <openroad-binary> [tag]}"
TAG="${2:-vibeic-eda:0.3.47}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT

EXPECT_OPENROAD_VERSION="26Q3-2065-gcad013df98"

[ -f "$OPENROAD_BIN" ] || { echo "no such openroad binary: $OPENROAD_BIN" >&2; exit 1; }

mkdir -p "$CTX/pdk" "$CTX/bin"
cp -a "$REPO_DIR/tools/pdk/." "$CTX/pdk/"
cp "$OPENROAD_BIN" "$CTX/bin/openroad"
chmod 0755 "$CTX/bin/openroad"

echo "context:   $CTX"
echo "openroad:  $OPENROAD_BIN"
echo "           sha256 $(sha256sum "$CTX/bin/openroad" | cut -d' ' -f1)"
echo "           expecting -version to read $EXPECT_OPENROAD_VERSION"
echo "tag:       $TAG"

docker build -f "$REPO_DIR/Dockerfile.derived" -t "$TAG" "$CTX"

echo
echo "built $TAG"
docker image inspect "$TAG" \
  --format 'id={{.Id}}
version={{index .Config.Labels "org.opencontainers.image.version"}}
base={{index .Config.Labels "io.vibeic.derived.base"}}'
