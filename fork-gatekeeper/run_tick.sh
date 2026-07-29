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
# THIS is the production runner (vibeic/vibeic-eda#12). GK_STATE_DIR says WHERE the state
# is; it does not say who is entitled to overwrite it, and before #12 nothing did — any
# checkout that imported assess_release and called assess() wrote the cache this tick
# reads. The declaration lives here, in the cron entrypoint, and nowhere else: a run that
# is not this script writes its own state (point GK_STATE_DIR elsewhere) or asks for the
# shared one on purpose. Exported, so the integration harness this tick spawns inherits it.
export GK_PRODUCTION_WRITER=1
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

# The fork-only rule, enforced HERE because GitHub Actions does not run on this
# repo. Measured 2026-07-29: `actions/runs` -> total_count 0, for every workflow
# including the pre-existing image-version-sync, while `actions/permissions`
# reports enabled. So `.github/workflows/fork-only.yml` is registered and never
# fires, and a rule enforced only there would be enforced nowhere.
#
# This tick already runs from the checked-out repo on a schedule, so it is the
# one place the checks reliably execute. It does NOT gate the tick: upstream
# tracking is useful whether or not a pin disagrees with itself, and coupling
# them would mean one bad pin silently stops the fork monitoring too. It records
# the verdict where the daily report can show it.
GUARD_OUT="${LOG_DIR}/source-guards.txt"
: > "${GUARD_OUT}"
guard_rc=0
for g in check_fork_only.py check_pins_agree.py; do
    if [ -f "${DIR}/../tools/${g}" ]; then
        log "[guard] ${g}"
        if ! python3 "${DIR}/../tools/${g}" >>"${GUARD_OUT}" 2>&1; then
            guard_rc=1
        fi
    else
        # A missing guard is not a passing guard. If tools/ moves or is renamed,
        # this must say so rather than quietly checking nothing.
        echo "MISSING: tools/${g} — nothing was checked" >> "${GUARD_OUT}"
        guard_rc=1
    fi
done
if [ "${guard_rc}" != "0" ]; then
    log "[guard] FAILED — the image would build from a source we do not control,"
    log "[guard] or a pin disagrees with itself. Details:"
    sed 's/^/[guard]   /' "${GUARD_OUT}" | tee -a "${LOG}"
else
    log "[guard] source guards clean"
    grep -E "pending|DATED" "${GUARD_OUT}" | sed 's/^/[guard]   /' | tee -a "${LOG}" || true
fi

# vibe-ic#553 — the OTHER direction. Everything below asks "should we adopt this
# upstream commit?"; this asks "is upstream carrying a fix for a bug we still
# ship?" Those are different questions and only the first was wired. Surfaced by
# vibe-ic#551: an rsz::stitchTrees segfault we were about to file upstream had
# been fixed there on 2026-07-13, 772 commits before our pin.
#
# Reporting only — it never adopts, and it does NOT gate the tick, for the same
# reason the source guards do not: a survey that cannot run must not stop the
# fork monitoring.
# ---------------------------------------------------------------------------
# THE DAILY DELIVERY CHAIN (owner rulings, 2026-07-29)
#
#   "daily merge all new commit from upstream for forked tools" / "每天自動合併、
#    不等人"  and  "只要有任何一個工具有新版，我們的 image 也就重新再出一版 …
#    最晚會在隔天早上的 5 點半，提供一個 daily 的新版 updated docker image"
#
# Four hops, and every one of them has been the place the work stopped:
#
#   upstream -> fork branch   daily_merge.py       merge everything, no PR
#   fork branch -> pin        daily_release.py     move the pin at all 3 sites
#   pin -> tool artefact      daily_release.py     rebuild what changed
#   artefact -> image         daily_release.py     recompose + cut a version
#
# On 2026-07-29 `vibeic/yosys#2` was merged and every run that day still used the
# pre-merge yosys, because hop 2 never happened and nothing looked. Running these
# in the tick is what makes the rulings take effect; until this block existed
# they were a policy nobody executed.
#
# NEITHER GATES THE TICK. A fork that conflicts is left at its previous tip and
# reported; upstream tracking and the gatekeeper round still run. What must never
# happen is a SILENT skip, so both write their own log and a non-zero exit is
# carried into the tick's exit code rather than discarded.
MERGE_OUT="${LOG_DIR}/daily-merge.txt"
if [ -f "${DIR}/daily_merge.py" ]; then
    log "[merge] merging all upstream commits into every fork"
    python3 "${DIR}/daily_merge.py" --json "${LOG_DIR}/daily-merge.json" \
        > "${MERGE_OUT}" 2>&1
    merge_rc=$?
    head -1 "${MERGE_OUT}" | sed 's/^/[merge]   /' | tee -a "${LOG}"
    grep -E "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM" "${MERGE_OUT}" \
        | sed 's/^/[merge]   /' | tee -a "${LOG}" || true
else
    echo "MISSING: fork-gatekeeper/daily_merge.py — nothing was merged" > "${MERGE_OUT}"
    log "[merge] MISSING — nothing was merged, which is not a clean result"
    merge_rc=1
fi

RELEASE_OUT="${LOG_DIR}/daily-release.txt"
if [ -f "${DIR}/daily_release.py" ]; then
    log "[release] moving pins to their fork tips, rebuilding what changed"
    python3 "${DIR}/daily_release.py" --json "${LOG_DIR}/daily-release.json" \
        > "${RELEASE_OUT}" 2>&1
    release_rc=$?
    head -1 "${RELEASE_OUT}" | sed 's/^/[release]   /' | tee -a "${LOG}"
    grep -E "VERSION|building|composing|UNRESOLVED|FAIL" "${RELEASE_OUT}" \
        | sed 's/^/[release]   /' | tee -a "${LOG}" || true
else
    echo "MISSING: fork-gatekeeper/daily_release.py — nothing was released" \
        > "${RELEASE_OUT}"
    log "[release] MISSING — no image was cut, which is not a clean result"
    release_rc=1
fi

# Does the tool the flow INVOKES come from the artefact we built? Every other
# gate stops at "the image contains our tool", which is a different claim and was
# true for months while verilator ran the base image's April build (#18) and
# klayout's LEF/DEF plugin was loaded by nothing but svrfdrc (#17).
#
# REPORT-ONLY on purpose. #17 and #18 are open and would fail it; a gate whose
# first act is to stop every daily release is a gate someone switches off. It
# takes --strict once they are closed.
REACH_OUT="${LOG_DIR}/fork-reaches-flow.txt"
if [ -f "${DIR}/fork_reaches_flow_check.py" ]; then
    REACH_IMG="ghcr.io/vibeic/vibeic-eda:local"
    if docker image inspect "${REACH_IMG}" >/dev/null 2>&1; then
        log "[reach] checking the composed image runs OUR builds"
        python3 "${DIR}/fork_reaches_flow_check.py" "${REACH_IMG}" \
            --json "${LOG_DIR}/fork-reaches-flow.json" > "${REACH_OUT}" 2>&1 || true
        head -1 "${REACH_OUT}" | sed 's/^/[reach]   /' | tee -a "${LOG}"
        grep -E "resolves outside|not on PATH" "${REACH_OUT}" \
            | sed 's/^/[reach]   /' | tee -a "${LOG}" || true
    else
        # No composed image is not a pass. Say which image was missing.
        echo "MISSING IMAGE: ${REACH_IMG} — nothing was checked" > "${REACH_OUT}"
        log "[reach] ${REACH_IMG} absent — nothing was checked, not a clean result"
    fi
else
    echo "MISSING: fork-gatekeeper/fork_reaches_flow_check.py" > "${REACH_OUT}"
    log "[reach] MISSING — nothing was checked, which is not a clean result"
fi

INBOUND_OUT="${LOG_DIR}/inbound-survey.txt"
if [ -f "${DIR}/inbound_survey.py" ]; then
    log "[inbound] surveying upstream fixes our pins lack"
    if python3 "${DIR}/inbound_survey.py" \
            --json "${LOG_DIR}/inbound-survey.json" > "${INBOUND_OUT}" 2>&1; then
        head -1 "${INBOUND_OUT}" | sed 's/^/[inbound]   /' | tee -a "${LOG}"
        grep -E "SAMPLED|NOTE:" "${INBOUND_OUT}" | sed 's/^/[inbound]   /' \
            | tee -a "${LOG}" || true
    else
        log "[inbound] survey FAILED — details in ${INBOUND_OUT}"
    fi
else
    # Same rule as the guards above: a missing survey is not an empty gap.
    echo "MISSING: fork-gatekeeper/inbound_survey.py — nothing was surveyed" \
        > "${INBOUND_OUT}"
    log "[inbound] MISSING — nothing was surveyed, which is not a clean result"
fi

log "[start] eda-fork gatekeeper tick (merge-pr=${GK_MERGE_PR})"
cd "${DIR}" || exit 2
python3 gatekeeper.py >>"${LOG}" 2>&1
rc=$?
# A guard failure must not be erased by a successful tick.
[ "${guard_rc}" != "0" ] && [ "${rc}" = "0" ] && rc=3
# Nor by a merge or a release that needed a human. A tick that merged nothing and
# cut no image because a fork conflicted has not had a clean day.
[ "${merge_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=4
[ "${release_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=5
log "[done] gatekeeper tick exit ${rc}"
exit ${rc}
