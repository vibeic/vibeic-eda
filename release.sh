#!/usr/bin/env bash
# Retired manual release helper.
#
# Publishing a version tag and ``:latest`` without atomically publishing
# RELEASED.json created a second release authority.  A host lock prevents that
# helper from racing daily_release, but cannot make its missing record true; the
# next noop could even interpret the old record as authority to roll ``:latest``
# backward.  There is now exactly one final-image publisher.
set -euo pipefail

VER="${1:?usage: ./release.sh <version>  e.g. ./release.sh 0.2.5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[REFUSED] release.sh cannot publish ${VER}: it cannot establish the " \
     "immutable/latest/RELEASED.json/anchor terminal proof." >&2
echo "Use the canonical transaction instead:" >&2
echo "  python3 ${SCRIPT_DIR}/fork-gatekeeper/daily_release.py --eda-root ${SCRIPT_DIR}" >&2
exit 2
