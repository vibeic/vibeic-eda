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

# Is this round even running the code that was landed for it?
#
# Nothing here or in run_tick.sh updates this checkout, so a fix landed in
# fork-gatekeeper/ stays invisible to the thing that runs it. Measured 2026-08-03:
# five programs behind origin/main, including discover_forks.py and build_page.py —
# the two that produce the published numbers — and among the missed commits was the
# one that changed HOW they are computed, after the old reference was shown to both
# overcount and undercount in one sweep. The round would have put today's date on a
# number produced by a method already known to be wrong.
#
# Placed before the publish and deliberately NOT gating the MERGE half: bringing
# upstream into our line is useful even from slightly older code, and blocking it
# would trade a reporting problem for a synchronisation one.
python3 "${DIR}/check_round_code_is_current.py" >> "${LOG}" 2>&1
CODE=$?
echo "[$(date -Is)] check_round_code_is_current exit ${CODE}" >> "${LOG}"

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

# `CODE` -eq 1 means a program that computes what we publish is superseded. A page
# built from it carries today's date over yesterday's method, which is harder to
# notice than yesterday's page — the same reasoning the ledger branch below already
# encodes. rc=2 (could not tell) does NOT block: a transient fetch failure and a
# proven-stale program are different risks, and only the second earns a dark page.
if [ "${DISC}" -eq 0 ] && [ "${CODE}" -ne 1 ]; then
    python3 "${DIR}/build_page.py" --out /home/reyerchu/vibeic.ai/eda-forks.html >> "${LOG}" 2>&1
    PAGE=$?
    echo "[$(date -Is)] build_page exit ${PAGE}" >> "${LOG}"
else
    # Publishing a page built from a ledger that failed to refresh would put stale
    # numbers under today's date, which is worse than leaving yesterday's page up.
    #
    # PAGE stays 0 so the skip alone does not fail the round — DISC already does
    # that below — but NO `build_page exit 0` line is written. That line would say
    # the page build succeeded on a morning it never ran, and anything grepping
    # `build_page exit` (rather than reading the log in order) would read it as a
    # healthy publish. A step that did not run reports that it did not run.
    PAGE=0
    if [ "${CODE}" -eq 1 ]; then
        echo "[$(date -Is)] build_page SKIPPED — this checkout runs superseded code; see check_round_code_is_current above" >> "${LOG}"
    else
        echo "[$(date -Is)] build_page SKIPPED — the ledger did not refresh" >> "${LOG}"
    fi
fi

# THE ROUND MUST NOTICE ITS OWN SILENCE (vibeic-eda#58). Three consecutive days
# published nothing and raised no alert: the round exited 1 into a log, and the
# only visible symptom was a public page that did not move — noticed by a person,
# on day three. Every step above reports ITS OWN exit; none of them answers "did
# today's numbers actually reach the ledger?", which is the question a reader of
# the page is really asking.
#
# It runs LAST and UNCONDITIONALLY — after a failed publish is exactly when it
# matters, so it must not sit behind any of the exits above.
# THE TWO DAILY NUMBERS ARE MEASURED BY ONE PROGRAM, ALWAYS THE SAME WAY.
# They were measured by hand on 2026-08-02 and the hand got them wrong twice in
# one evening: a missing pin fell back to the clone's HEAD, which makes the gap
# identically 0, and "behind" was read as one number when it is two (SYNC lag vs
# RELEASE lag, which need opposite fixes). Both mistakes produced a reassuring
# answer. A measurement that has to be remembered is not a measurement.
python3 "${DIR}/fork_gap_report.py" \
    --json "${STATE:-$HOME/.cache/eda-fork-gatekeeper}/fork_gap.json" >> "${LOG}" 2>&1
GAP=$?
echo "[$(date -Is)] fork_gap_report exit ${GAP}" >> "${LOG}"

python3 "${DIR}/check_ledger_is_fresh.py" \
    --json "${STATE:-$HOME/.cache/eda-fork-gatekeeper}/ledger_freshness.json" >> "${LOG}" 2>&1
FRESH=$?
echo "[$(date -Is)] check_ledger_is_fresh exit ${FRESH}" >> "${LOG}"

# 0 only when BOTH are clean; the six steps' own 1 means "a case still needs a
# human", which is information, not noise.
if [ "${SIX}" -ne 0 ] || [ "${TICK}" -ne 0 ] || [ "${DISC}" -ne 0 ] \
   || [ "${PAGE}" -ne 0 ] || [ "${FRESH}" -ne 0 ] || [ "${CODE}" -ne 0 ]; then exit 1; fi
exit 0
