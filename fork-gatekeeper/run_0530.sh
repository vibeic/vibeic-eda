#!/usr/bin/env bash
# The 05:30 entry point on 8HD-d. ONE cron line, two pipelines, in order.
#
#   1. daily_0530.py --apply  — the six steps over every fork: upstream into
#      our line, our branches into our line, the merge/cherry-pick conflicts
#      handed to THIS HOST'S GATEKEEPER AI (step 2b), then prune.
#   2. run_tick.sh            — the pre-existing plugin-repo gatekeeper tick.
#      Kept, not replaced: it answers a different question (the plugin repo's
#      version gate), and it was already the only thing on this cron line.
#
# Exit codes are captured DIRECTLY, never through a pipe. `cmd | tee; echo $?`
# reports tee's status, which has already caused two false "clean" reports on
# this campaign.
set -uo pipefail
export PATH="${HOME}/.local/bin:/home/reyerchu/.nvm/versions/node/v22.22.0/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/reyerchu}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
export GK_PRODUCTION_WRITER=1
LOG="${GK_STATE_DIR}/daily_0530.log"
mkdir -p "${GK_STATE_DIR}"

LOCK="${GK_STATE_DIR}/daily_0530.lock"
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "[$(date -Is)] [skip] a 05:30 round is already running" >> "${LOG}"
    exit 0
fi

{
  echo "════════ $(date -Is) 05:30 round ════════"
} >> "${LOG}"

# Step 1-4 + the AI half. --ai-timeout is generous: the gatekeeper may have to
# read real code across several forks, and this call BLOCKS on purpose.
python3 "${DIR}/daily_0530.py" --apply --ai-timeout 5400 \
        --json "${GK_STATE_DIR}/daily_0530.json" >> "${LOG}" 2>&1
SIX=$?
echo "[$(date -Is)] daily_0530 exit ${SIX}" >> "${LOG}"

# Step 5 — build + verify + publish, and the plugin-repo gatekeeper tick.
"${DIR}/run_tick.sh" >> "${GK_STATE_DIR}/cron.log" 2>&1
TICK=$?
echo "[$(date -Is)] run_tick exit ${TICK}" >> "${LOG}"

# Step 6 — refresh the fork ledger and REGENERATE the public page.
#
# This step did not exist. The cron merged, built, verified and published every
# morning, and never touched https://vibeic.ai/eda-forks.html — so the page only
# ever changed when a human regenerated it by hand, and silently described an
# older world in between. A daily pipeline whose public output is updated
# manually is not a daily pipeline.
#
# It runs AFTER run_tick so the page reflects the release that just shipped.
# Exit status captured directly, per the note at the top of this file.
python3 "${DIR}/discover_forks.py" >> "${LOG}" 2>&1
DISC=$?
echo "[$(date -Is)] discover_forks exit ${DISC}" >> "${LOG}"

if [ "${DISC}" -eq 0 ]; then
    python3 "${DIR}/build_page.py" --out /home/reyerchu/vibeic.ai/eda-forks.html >> "${LOG}" 2>&1
    PAGE=$?
else
    # Publishing a page built from a ledger that failed to refresh would put stale
    # numbers under today's date, which is worse than leaving yesterday's page up.
    PAGE=0
    echo "[$(date -Is)] build_page SKIPPED — the ledger did not refresh" >> "${LOG}"
fi
echo "[$(date -Is)] build_page exit ${PAGE}" >> "${LOG}"

# 0 only when BOTH are clean; the six steps' own 1 means "a case still needs a
# human", which is information, not noise.
if [ "${SIX}" -ne 0 ] || [ "${TICK}" -ne 0 ] || [ "${DISC}" -ne 0 ] || [ "${PAGE}" -ne 0 ]; then exit 1; fi
exit 0
