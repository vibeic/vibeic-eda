#!/usr/bin/env bash
# prepush-gates.sh — the checks that already exist, run BEFORE a push instead of
# the next morning.
#
# WHY (vibeic-eda#41 / #42). `main` named an unpublished klayout tag and
# `bake eda` could not build. `tools/check_pins_agree.py` catches that exactly —
# run against the pre-fix commit it returns rc 1 and names both tags. The defect
# was never a missing check. NOTHING ASKED IT BEFORE THE PUSH.
#
# Every path that did run it ran after the fact:
#
#   fork-gatekeeper/run_tick.sh:78    daily 05:30, blocking, but the next morning
#   fork-gatekeeper/run_tick.sh:186   after daily_release writes pins — REPORT-ONLY,
#                                     the image is already published by then
#   .github/workflows/*.yml           GitHub Actions is not our CI
#
# run_tick.sh:178 says so in its own comment: "a disagreement introduced by the
# release itself would go unnoticed until the next morning. That is not
# hypothetical."
#
# THIS IS ONE MACHINE'S WORD. A hook lives in .git/hooks, does not travel with a
# clone, and `--no-verify` bypasses it silently. It is not a substitute for the
# tick, which still runs everything on a machine nobody edited; it moves the
# cheap half of that answer to the moment the mistake is made, where it costs
# seconds instead of a broken main for a day.
#
# Usage:  tools/prepush-gates.sh [--offline]
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
OFFLINE=0
FULL=0
BASE="${PREPUSH_BASE:-origin/main}"
for a in "$@"; do
  case "$a" in
    --offline) OFFLINE=1 ;;
    --full)    FULL=1 ;;
  esac
done

FAILED=0
run() {                                   # run <label> <cmd…>
  local label="$1"; shift
  local out t0 t1
  t0=$(date +%s%N)
  if out="$("$@" 2>&1)"; then
    t1=$(date +%s%N)
    printf '  PASS  %-34s %5d ms\n' "$label" $(( (t1-t0)/1000000 ))
  else
    t1=$(date +%s%N)
    printf '  FAIL  %-34s %5d ms\n' "$label" $(( (t1-t0)/1000000 ))
    # FAILING lines first. An aggregating check puts its failure in the middle
    # and its summary at the end, so `tail` alone shows the wrong thing.
    printf '%s\n' "$out" | grep -E '^\[FAIL\]|disagree|does not have|do not reproduce' \
      | head -8 | sed 's/^/          /'
    printf '%s\n' "$out" | tail -3 | sed 's/^/          /'
    FAILED=1
  fi
}

echo "=== pre-push gates — $(git -C "$ROOT" rev-parse --short HEAD) ==="

# CHEAP TIER: ~1s total, measured. A pre-push check that costs a minute is one
# people learn to skip, so the cost is stated rather than assumed.
run "pins agree in all three places" python3 "$ROOT/tools/check_pins_agree.py"
run "every clone is a vibeic fork"   python3 "$ROOT/tools/check_fork_only.py"
run "README counts reproduce"        python3 "$ROOT/tools/check_doc_counts.py"
run "fork presence claims hold"      python3 "$ROOT/tools/check_fork_presence_claims.py"

# REGISTRY TIER, run only when a PIN MOVED. This is the half a source-only check
# cannot do — three pin sites can agree perfectly and name an image nobody
# pushed (#40) — but it is one round-trip per pin and measured between 8s and
# 42s depending on the registry, against ~0.9s for everything above.
#
# Gating it on the pin files rather than always: a push that does not touch a
# pin cannot introduce an unpublished tag, and a check that adds 40s to every
# push is one people learn to skip. `--full` forces it; the daily tick runs it
# unconditionally, which is where the unconditional answer belongs.
PIN_FILES_TOUCHED=0
if [ "$FULL" = "1" ]; then
  PIN_FILES_TOUCHED=1
elif git -C "$ROOT" diff --name-only "$BASE"...HEAD 2>/dev/null \
     | grep -qE '^(Dockerfile|docker-bake\.hcl|tools/[^/]+/Dockerfile)$'; then
  PIN_FILES_TOUCHED=1
fi

if [ "$OFFLINE" = "1" ]; then
  echo "  SKIP  pinned images exist              (--offline: NOT a clean result)"
elif [ "$PIN_FILES_TOUCHED" = "0" ]; then
  echo "  n/a   pinned images exist              (no pin file in $BASE..HEAD)"
else
  run "pinned images exist in registry" python3 "$ROOT/tools/check_pinned_images_exist.py"
fi

if [ "$FAILED" -eq 0 ]; then
  echo "=== all pre-push gates PASS ==="
else
  echo "=== FAILURES ABOVE — fix them, or push with --no-verify and own it ==="
fi
exit "$FAILED"
