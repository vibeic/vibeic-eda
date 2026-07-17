#!/usr/bin/env bash
# run_tick.sh — one daily EDA-fork gatekeeper round (cron entrypoint).
#
# Mirrors the awesome-open-ic enrich cron: flock so rounds never overlap, a fixed
# headless PATH/HOME, and gh OAuth resolved to GH_TOKEN (the vibeic org rejects
# long-lived fine-grained PATs; the gho_ OAuth token works headless). Then runs the
# gatekeeper tick, which re-seeds ledgers, decides merge/defer/clean per fork, writes
# the daily report, and regenerates the vibeic.ai monitor page.
#
# Scheduled by cron at 05:30 Asia/Taipei (UTC+8). Safe to run by hand.
set -uo pipefail

export PATH="${HOME}/.local/bin:/home/reyerchu/.nvm/versions/node/v22.22.0/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/reyerchu}"

# SOURCE (version-controlled, canonical) is this script's own directory — so the cron
# runs whatever is checked into vibeic-eda/fork-gatekeeper/, no separate deployed copy
# to drift. RUNTIME STATE (ledgers, reports, locks, logs) lives in the cache dir,
# OUTSIDE the source tree, so running in place never dirties the repo.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
LOG_DIR="${GK_STATE_DIR}"
LOCK="${LOG_DIR}/tick.lock"
LOG="${LOG_DIR}/tick.log"
mkdir -p "${LOG_DIR}"

log() { echo "[$(date -Is)] $*" | tee -a "${LOG}"; }

exec 9>"${LOCK}"
if ! flock -n 9; then
    log "[skip] another gatekeeper round is running"
    exit 0
fi

# gh OAuth token → GH_TOKEN (headless), like the enrich cron.
TOKEN="$(gh auth token 2>/dev/null || true)"
[ -z "${TOKEN}" ] && TOKEN="$(cat "${HOME}/.config/github/token" 2>/dev/null || true)"
if [ -z "${TOKEN}" ]; then
    log "[fatal] no GitHub token (gh auth / token file both empty)"
    exit 2
fi
export GH_TOKEN="${TOKEN}"

# ARMED: when a fork is behind, after the (safe, tool-less) LLM usefulness judgment, open a
# cherry-pick MERGE PR for the clearly-safe commits (real upstream commits, human-reviewed,
# never auto-merged, never force-push — reviewed push-safe). Only fires on a behind fork; opens
# nothing on a clean day. Set GK_MERGE_PR=0 to disable.
export GK_MERGE_PR="${GK_MERGE_PR:-1}"

log "[start] eda-fork gatekeeper tick (merge-pr=${GK_MERGE_PR})"
cd "${DIR}" || exit 2
python3 gatekeeper.py >>"${LOG}" 2>&1
rc=$?
log "[done] gatekeeper tick exit ${rc}"
exit ${rc}
