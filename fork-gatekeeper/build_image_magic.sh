#!/usr/bin/env bash
set -uo pipefail
SCRATCH=/tmp/gk-eda-build-magic
TAG=vibeic-eda-gkcandidate:magic-8.3.675
LOG=/home/reyerchu/eda-fork-gatekeeper/reports/image_build_magic.log
: > "$LOG"
echo "[$(date -Is)] docker build ${TAG} (MAGIC_REF=$(grep -m1 '^ARG MAGIC_REF' $SCRATCH/Dockerfile|cut -d= -f2|cut -c1-12))" | tee -a "$LOG"
docker build -t "${TAG}" "${SCRATCH}" >>"$LOG" 2>&1
rc=$?
echo "[$(date -Is)] docker build exit ${rc}" | tee -a "$LOG"
if [ "$rc" = 0 ]; then
  docker run --rm --entrypoint bash "${TAG}" -lc 'magic --version && netgen -batch quit </dev/null 2>&1 | head -1; yosys -V | head -1 && echo SMOKE_OK' >>"$LOG" 2>&1
  echo "[$(date -Is)] smoke exit $?" | tee -a "$LOG"
fi
exit ${rc}
