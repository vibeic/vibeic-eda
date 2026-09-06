#!/usr/bin/env bash
# Build the 0.3.48 derived image from the 0.3.47 digest.
#
# This exists because the release composition cannot be built on the hosts that
# need the image: no credential here has read:packages for the private
# eda-tool-* artefacts. Owner ruling 2026-09-07.
#
# It stages a build context rather than using the repo as one, because the
# ~145 MB openroad binary must not be committed to this repository. Unlike the
# 0.3.47 build the context is JUST that binary: every PDK correction is already
# in the base image's layers and is inherited, not re-applied.
#
# THE BASE IS A DIGEST; ITS REPOSITORY IS DEPLOYMENT CONFIGURATION and is read
# from $VIBEIC_EDA_IMAGE_REPO, exactly the way the plugin's `_eda_pin.py` reads
# it. 0.3.47 was pushed to the fleet's private registry and never to ghcr, so
# the ghcr default cannot serve it; a host that has the bytes has them under
# whatever repository it pulled them from, and that is the one thing this script
# must be told rather than assume. No registry address is written into this
# repository.
#
# Usage:
#   tools/build_derived.sh <path-to-openroad-binary> [tag]
set -euo pipefail

OPENROAD_BIN="${1:?usage: build_derived.sh <openroad-binary> [tag]}"
TAG="${2:-vibeic-eda:0.3.48}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT

BASE_DIGEST="sha256:8da785a8d3275884ad0d0ee0fb10f7e90d8b7bf11a08d38e9559b0764112480f"
BASE_REPO="${VIBEIC_EDA_IMAGE_REPO:-ghcr.io/vibeic/vibeic-eda}"
BASE_IMAGE="${BASE_REPO}@${BASE_DIGEST}"
EXPECT_OPENROAD_VERSION="26Q3-2070-gfbfe6cae4d"
EXPECT_OPENROAD_COMMIT="fbfe6cae4d8aa1e1291d1bb6aad3055a04efb7e8"

[ -f "$OPENROAD_BIN" ] || { echo "no such openroad binary: $OPENROAD_BIN" >&2; exit 1; }

# THE BASE MUST BE THE BYTES, NOT A NAME THAT RESOLVES. `--pull=false` is not
# enough: a repository that serves a DIFFERENT manifest under this digest cannot
# exist, but a repository that serves nothing looks identical to a typo until the
# build fails 40 minutes in. Ask now.
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || {
  echo "the base image is not present locally as $BASE_IMAGE" >&2
  echo "  pull it by digest first, or set VIBEIC_EDA_IMAGE_REPO to a registry that serves it" >&2
  exit 1; }

mkdir -p "$CTX/bin"
cp "$OPENROAD_BIN" "$CTX/bin/openroad"
chmod 0755 "$CTX/bin/openroad"

echo "context:   $CTX"
echo "base:      $BASE_IMAGE"
echo "openroad:  $OPENROAD_BIN"
echo "           sha256 $(sha256sum "$CTX/bin/openroad" | cut -d' ' -f1)"
echo "           expecting -version to read $EXPECT_OPENROAD_VERSION ($EXPECT_OPENROAD_COMMIT)"
echo "tag:       $TAG"

docker build -f "$REPO_DIR/Dockerfile.derived" \
  --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$TAG" "$CTX"

echo
echo "built $TAG"
docker image inspect "$TAG" \
  --format 'id={{.Id}}
version={{index .Config.Labels "org.opencontainers.image.version"}}
base={{index .Config.Labels "io.vibeic.derived.base"}}
openroad={{index .Config.Labels "io.vibeic.derived.openroad.commit"}}'
