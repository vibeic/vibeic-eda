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

# EXECUTOR mode. DISARMED by default (python = the deterministic assess→PR flow). The
# capability-separated Claude DECIDER is opt-in (GK_EXECUTOR=claude): a review found that an
# unattended bypassPermissions agent holding an org-write token while it reads UNTRUSTED
# upstream commit text is a critical prompt-injection / exfiltration hole. So here Claude only
# DECIDES (adopt/skip/defer) — in a process with the token STRIPPED, tools restricted to
# read + a single decision-file write, and its working root limited to the state dir (no repo,
# no shell, no push). A deterministic executor then RE-VALIDATES every decision against the
# trusted assessment before anything is acted on. Claude has judgment, never the keys.
GK_EXECUTOR="${GK_EXECUTOR:-python}"
GK_SHIP="${GK_SHIP:-prepare}"; export GK_SHIP
DATE="$(date +%F)"
cd "${DIR}" || exit 2
log "[start] eda-fork gatekeeper tick (executor=${GK_EXECUTOR}, ship=${GK_SHIP})"

# PHASE 1 (deterministic, holds token): refresh ledgers, assess upstream commits per behind
# fork, write the daily report + regenerate the page + open the human-review assessment PR.
python3 gatekeeper.py >>"${LOG}" 2>&1; rc=$?

# PHASE 2 (Claude DECIDER) + PHASE 3 (deterministic validator) — only when opted in AND there
# are assessments to decide on. On a clean day nothing here runs.
if [ "${GK_EXECUTOR}" = "claude" ] && command -v claude >/dev/null 2>&1 \
   && ls "${GK_STATE_DIR}/reports/assessments/${DATE}-"*.json >/dev/null 2>&1; then
    mkdir -p "${GK_STATE_DIR}/decisions"
    log "[phase2] decider — token STRIPPED, tools=Read/Grep/Glob/Write, root=state dir only"
    DECIDER_MISSION="$(cat "${DIR}/DECIDER.md")
---
Today is ${DATE}. GK_STATE_DIR=${GK_STATE_DIR}. Read the assessment JSON(s) at ${GK_STATE_DIR}/reports/assessments/${DATE}-*.json and write your decisions to ${GK_STATE_DIR}/decisions/${DATE}.json. That is the only file you write."
    # SECURITY (capability separation): strip every git credential from the decider's env; give
    # it NO shell (no Bash → cannot run env/printenv/curl/gh/git); restrict its filesystem root
    # to the state dir (assessments in, decisions out) — NOT the fork clones or the image repo;
    # bound its runtime. It reads untrusted upstream text with no keys and no way to act.
    env -u GH_TOKEN -u GITHUB_TOKEN timeout 900 claude -p "${DECIDER_MISSION}" \
        --permission-mode default \
        --allowedTools "Read" "Grep" "Glob" "Write" \
        --add-dir "${GK_STATE_DIR}" >>"${LOG}" 2>&1 || log "[phase2] decider exit non-zero (ignored)"
    # PHASE 3 (deterministic, holds token): RE-VALIDATE the decisions against the trusted
    # assessment — an adopt outside the clean-safe set is rejected. v1 validates + records;
    # acting on the validated adopts (cherry-pick/build/ship) is a further-gated step.
    log "[phase3] validate decisions against the trusted assessment"
    python3 execute_decisions.py "${DATE}" >>"${LOG}" 2>&1 || true
fi

log "[done] eda-fork gatekeeper tick exit ${rc}"
exit ${rc}
