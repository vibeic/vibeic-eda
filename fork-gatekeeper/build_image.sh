#!/usr/bin/env bash
# Full vibeic-eda image build with the resolved-yosys pin (local candidate; NO registry push).
set -uo pipefail
SCRATCH=/tmp/gk-eda-build
TAG=vibeic-eda-gkcandidate:yosys-v0.67
LOG=/home/reyerchu/eda-fork-gatekeeper/reports/image_build.log
: > "$LOG"
echo "[$(date -Is)] docker build ${TAG} (YOSYS_REF=$(grep -m1 '^ARG YOSYS_REF' $SCRATCH/Dockerfile | cut -d= -f2))" | tee -a "$LOG"
docker build -t "${TAG}" "${SCRATCH}" >>"$LOG" 2>&1
rc=$?
echo "[$(date -Is)] docker build exit ${rc}" | tee -a "$LOG"
if [ "$rc" = 0 ]; then
  echo "--- smoke: tools run in the fresh image ---" | tee -a "$LOG"
  docker run --rm --entrypoint bash "${TAG}" -lc 'yosys -V && openroad -version && echo SMOKE_OK' >>"$LOG" 2>&1
  echo "[$(date -Is)] smoke exit $?" | tee -a "$LOG"
fi
exit ${rc}
