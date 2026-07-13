#!/usr/bin/env bash
# Verify the yosys conflict resolution by replicating the vibeic-eda Dockerfile's
# yosys stage EXACTLY (ubuntu:24.04 + the same apt deps + the same cmake build) on
# the resolved candidate branch, then running our tribuf regression test. v0.67 uses
# CMake (>=3.28) — the Dockerfile already builds with cmake, so the recipe is unchanged;
# this proves our resolved patch compiles + works under v0.67.
set -uo pipefail
LOG=/home/reyerchu/eda-fork-gatekeeper/reports/verify_yosys.log
BR=vibeic/synth-fixes-v0.67
: > "$LOG"
echo "[$(date -Is)] Dockerfile-parity cmake build of ${BR} (ubuntu:24.04)" | tee -a "$LOG"
docker run --rm ubuntu:24.04 bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  echo "--- apt deps ---"
  apt-get update -q >/dev/null && apt-get install -y --no-install-recommends \
      build-essential cmake git bison flex gawk pkg-config \
      libreadline-dev tcl-dev libffi-dev zlib1g-dev python3 \
      libboost-system-dev libboost-python-dev libboost-filesystem-dev ca-certificates >/dev/null
  echo "cmake: $(cmake --version | head -1)"
  echo "--- clone '"$BR"' ---"
  git clone -q --branch '"$BR"' --single-branch https://github.com/vibeic/yosys.git /yosys
  cd /yosys && echo "HEAD: $(git rev-parse --short HEAD)"
  git submodule update --init --recursive 2>&1 | tail -2
  echo "--- cmake configure + build ---"
  cmake -S /yosys -B /yosys/build -DCMAKE_BUILD_TYPE=Release >/tmp/cfg.log 2>&1 || { tail -20 /tmp/cfg.log; exit 3; }
  cmake --build /yosys/build -j"$(nproc)" >/tmp/bld.log 2>&1 || { echo "BUILD FAILED:"; grep -iE "error:|synth.cc" /tmp/bld.log | head -20; exit 4; }
  echo "=== BUILD OK ==="
  YBIN=$(find /yosys/build -maxdepth 3 -name yosys -type f -executable | head -1)
  echo "yosys binary: $YBIN"
  "$YBIN" -V
  echo "--- tribuf regression test ---"
  "$YBIN" /yosys/tests/various/synth_tribuf.ys 2>&1 | tail -30
  echo "=== TEST DONE ==="
' >>"$LOG" 2>&1
rc=$?
echo "[$(date -Is)] verify_yosys exit ${rc}" | tee -a "$LOG"
exit ${rc}
