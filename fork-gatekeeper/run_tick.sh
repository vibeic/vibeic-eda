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
# check_doc_counts.py gets `--online` HERE and nowhere else. Two of the README's
# counts are properties of the GitHub org (its fork count, and how many distinct
# upstreams those forks have) — no checkout can verify them, and they are the two
# most likely to rot, since forking one repo changes both. This tick already holds
# a `gh` token and already runs daily, so it is the only place that can notice.
# The PR workflow runs the same checker offline, where those rows report as
# unverified rather than passing.
for g in check_fork_only.py check_pins_agree.py check_doc_counts.py check_fork_presence_claims.py; do
    case "${g}" in
        check_doc_counts.py) extra=(--online) ;;
        *)                   extra=() ;;
    esac
    if [ -f "${DIR}/../tools/${g}" ]; then
        log "[guard] ${g} ${extra[*]}"
        if ! python3 "${DIR}/../tools/${g}" "${extra[@]}" >>"${GUARD_OUT}" 2>&1; then
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
# RE-RUN THE PIN GUARD, because `daily_release` is the thing that writes pins.
# The guards at the top of this tick read the tree as it was BEFORE the release
# moved anything; a disagreement introduced by the release itself would go
# unnoticed until the next morning. That is not hypothetical — `check_pins_agree`
# went red on main today because the artefact tag gained a RECIPE component and
# the checker still composed the old two-part tag, and it was a PR that found it,
# not this tick.
#
# Report-only: the image is already built and published by this point, so failing
# here would not un-publish it. What it buys is knowing the same day.
POSTPIN_OUT="${LOG_DIR}/pins-after-release.txt"
if [ -f "${DIR}/../tools/check_pins_agree.py" ]; then
    if python3 "${DIR}/../tools/check_pins_agree.py" > "${POSTPIN_OUT}" 2>&1; then
        log "[pins-after] $(tail -1 "${POSTPIN_OUT}")"
    else
        log "[pins-after] DISAGREEMENT introduced by this release — details in ${POSTPIN_OUT}"
        grep -E "disagrees|pulls|pins " "${POSTPIN_OUT}" | head -6 | sed 's/^/[pins-after]   /' | tee -a "${LOG}" || true
    fi
else
    echo "MISSING: tools/check_pins_agree.py — nothing was re-checked" > "${POSTPIN_OUT}"
    log "[pins-after] MISSING — nothing was re-checked, which is not a clean result"
fi

# Derived ONCE, before every consumer. It used to be computed inside the
# `if [ -f fork_reaches_flow_check.py ]` branch while two later checks read it —
# so a missing checker left REACH_IMG unset, and under this script's `set -u`
# that is not a silent skip but an unbound-variable abort of the whole tick.
# A variable that three checks depend on does not belong inside one of them.
REL_VER="$(python3 -c 'import json;print(json.load(open("'"${DIR}"'/../RELEASED.json"))["version"])' 2>/dev/null || true)"
REACH_IMG=""
[ -n "${REL_VER}" ] && REACH_IMG="ghcr.io/vibeic/vibeic-eda:${REL_VER}"

REACH_OUT="${LOG_DIR}/fork-reaches-flow.txt"
if [ -f "${DIR}/fork_reaches_flow_check.py" ]; then
    # The image RELEASED.json says we last published — not `:local`, which was
    # `eda-local`'s tag and stopped being produced when the compose started
    # tagging by version (0.2.33). The stale `:local` from before that was still
    # on this host, so the check did not report a missing image: it quietly
    # inspected one from hours earlier and reported it as current. `--json` and
    # the provenance comparison are only meaningful against a FRESH image, which
    # is precisely what that made them not be.
    :  # REACH_IMG is derived above, before any of its consumers
    if [ -n "${REACH_IMG}" ] && docker image inspect "${REACH_IMG}" >/dev/null 2>&1; then
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
    python3 "${DIR}/inbound_survey.py" \
        --json "${LOG_DIR}/inbound-survey.json" > "${INBOUND_OUT}" 2>&1
    inbound_rc=$?
    # `inbound_survey.py` already distinguishes RC_PARTIAL (1 — some forks could
    # not be surveyed, the REST were) from RC_NOTHING (2). This branch treated
    # both as failure and printed only "survey FAILED", discarding the report it
    # had just written to disk.
    #
    # MEASURED on the 2026-07-30 05:51 tick: two forks lack an upstream parent
    # (cadical, kissat), so rc was 1 — and a complete 44-line survey of the other
    # 14 tools went unread. It named 4 pins behind upstream including 20 slang
    # fixes, among them a parser crash on invalid checker port connections, and a
    # memory-safety fix in xschem. Nothing was hidden; the one line a reader sees
    # said the whole thing failed, so nobody looked at the file (vibeic-eda#34).
    #
    # A partial result is a result. What must not be lost is WHICH part failed,
    # so the errors are printed too rather than the report replacing them.
    if [ "${inbound_rc}" != "2" ]; then
        head -1 "${INBOUND_OUT}" | sed 's/^/[inbound]   /' | tee -a "${LOG}"
        grep -E "SAMPLED|NOTE:|behind +[1-9]" "${INBOUND_OUT}" \
            | sed 's/^/[inbound]   /' | tee -a "${LOG}" || true
    fi
    if [ "${inbound_rc}" = "1" ]; then
        log "[inbound] survey PARTIAL — the forks below could not be surveyed; every other tool IS reported above"
        grep -E "ERROR" "${INBOUND_OUT}" | head -5 | sed 's/^/[inbound]   /' \
            | tee -a "${LOG}" || true
    elif [ "${inbound_rc}" != "0" ]; then
        log "[inbound] survey produced NOTHING (rc=${inbound_rc}) — details in ${INBOUND_OUT}"
    fi
else
    # Same rule as the guards above: a missing survey is not an empty gap.
    echo "MISSING: fork-gatekeeper/inbound_survey.py — nothing was surveyed" \
        > "${INBOUND_OUT}"
    log "[inbound] MISSING — nothing was surveyed, which is not a clean result"
fi

# `tools/check_image_provenance.py` is wired ONLY into .github/workflows/release.yml,
# and that workflow has never run — `actions/runs` reports total_count 0 for this
# repo, with all three workflows `state=active` (vibe-ic#550). A guard that
# shipped, is documented, behaves correctly, and executes nowhere is this repo's
# own failure applied to a gate. The tick is the one thing that reliably runs,
# which is the same reasoning that moved the source guards here.
# Did we take something AWAY? Every other check here asks what we added — that
# sources are ours, that a pin is coherent, that the flow runs our build. Three
# times this week a replacement removed a co-tenant instead: eqy/mcy and sby with
# the yosys prefix (#19), the yices solvers with the same rm -rf (#25), and the
# slang plugin left behind by a yosys upgrade (#24). Nothing was looking.
#
# BLOCKING (vibeic-eda#88). This ran with a trailing `|| true` until 2026-08-05,
# so it printed `LOST:` lines and the round carried on — a check that LOOKS like
# a gate and is not one, which is worse than no gate because the log reads clean
# to anyone scanning for a failure. The shape copied here is `capability_gate.py`,
# which is wired correctly: regression.json -> image_build.cmd -> gatekeeper.py
# -> build_and_regress.sh, where rc!=0 emits `built_red` and nothing is promoted.
#
# rc=2 BLOCKS EXACTLY LIKE rc=1, in both the program and this call site. rc=2 is
# "the image could not be probed" — nothing was compared, and measuring nothing
# proves nothing. The same rule already governs [ship] (rc 6) and the source
# guards (rc 3) in this file; the `|| true` here was the outlier.
#
# The status is captured on the SAME line as the command, before `head`/`grep`
# can overwrite $?. Writing `python3 ... ; cap_rc=$?` on the next line after a
# pipe is how build_and_regress.sh's first draft read the status of the wrong
# process, and this file's own `[ship]` block spells the rule out.
CAP_OUT="${LOG_DIR}/capability-lost.txt"
cap_rc=0
if [ -f "${DIR}/check_no_capability_lost.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[capability] ${REACH_IMG}"
    python3 "${DIR}/check_no_capability_lost.py" "${REACH_IMG}" \
        --json "${LOG_DIR}/capability-lost.json" > "${CAP_OUT}" 2>&1; cap_rc=$?
    head -1 "${CAP_OUT}" | sed 's/^/[capability]   /' | tee -a "${LOG}"
    grep "LOST:" "${CAP_OUT}" | sed 's/^/[capability]   /' | tee -a "${LOG}" || true
    [ "${cap_rc}" != "0" ] && log "[capability] rc=${cap_rc} — a command the base image provided no longer resolves in ours, or the image could not be probed at all (vibeic-eda#88)"
else
    echo "MISSING: check_no_capability_lost.py or no released image — nothing was checked" \
        > "${CAP_OUT}"
    log "[capability] nothing was checked, which is not a clean result"
    # Same rule as the branch above: a checker that is not there, or a release
    # with no image to probe, compared nothing. That is rc=2's meaning, so it
    # gets rc=2 rather than the silence it got before.
    cap_rc=2
fi

PROV_OUT="${LOG_DIR}/image-provenance.txt"
if [ -f "${DIR}/../tools/check_image_provenance.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[provenance] ${REACH_IMG}"
    python3 "${DIR}/../tools/check_image_provenance.py" "${REACH_IMG}" \
        > "${PROV_OUT}" 2>&1 || true
    tail -1 "${PROV_OUT}" | sed 's/^/[provenance]   /' | tee -a "${LOG}"
else
    echo "MISSING: tools/check_image_provenance.py or no released image — nothing was checked" \
        > "${PROV_OUT}"
    log "[provenance] nothing was checked, which is not a clean result"
fi

# --- NOTHING UNTRACKED AND UNIGNORED (2026-07-30) ---
# Sixteen files sat here untracked-and-unignored for eight to eleven days —
# `.bak` copies of the Dockerfile and the fork ledgers, build logs from a failed
# 0.2.27 attempt, an ALIGN dump. That is the state one `git add -A` turns into a
# commit, which is why this repo forbids `-A`; the prohibition is a rule
# everyone must remember, and this is the rule nobody has to.
#
# `.gitignore` already carried `*.log` and did not match
# `build_0.2.27.log.attempt1` — `.log` is not at the end. A rule written for the
# case its author met, which is why the guard checks the OUTCOME.
#
# Non-fatal: the tick's job is upstream tracking, and a stray file must not stop
# it. Loud in the log; the guard names paths and never deletes.
UNTRACKED_OUT="${LOG_DIR}/untracked-artefacts.txt"
if [ -f "${DIR}/untracked_artefact_guard.py" ]; then
    python3 "${DIR}/untracked_artefact_guard.py" > "${UNTRACKED_OUT}" 2>&1
    untracked_rc=$?
    tail -1 "${UNTRACKED_OUT}" | sed 's/^/[untracked]   /' | tee -a "${LOG}"
    [ "${untracked_rc}" != "0" ] && log "[untracked] rc=${untracked_rc} — ignore them or remove them; the guard will not choose"
else
    echo "MISSING: fork-gatekeeper/untracked_artefact_guard.py — nothing was checked" \
        > "${UNTRACKED_OUT}"
    log "[untracked] nothing was checked, which is not a clean result"
fi

# --- WHAT THE IMAGE SAYS ABOUT ITSELF (vibeic-eda#28) ---
# PDKS.json declares every PDK the image ships and where its version can be read
# back OUT of the image. This is the only guard with jurisdiction over them: every
# other one checks what we CLONE, and the PDKs arrive pre-installed in the base,
# so they are invisible to all of them by construction. open_pdks produces the
# libs.tech trees every DRC and LVS verdict is computed against.
#
# I LANDED THIS CHECK AND DID NOT WIRE IT. It has existed since 6f04ab6 and has
# never run — a gate that exists and is not called is the same as no gate, which
# is the defect it was written to prevent, one level up. Found by sweeping every
# program this session added for whether anything actually invokes it.
#
# Non-fatal like its neighbours: the tick's job is upstream tracking, and one
# undeclared PDK must not stop that. Loud in the log, and a missing checker
# reports rather than passes.
CLAIMS_OUT="${LOG_DIR}/image-claims.txt"
if [ -f "${DIR}/check_image_claims.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[claims] ${REACH_IMG}"
    python3 "${DIR}/check_image_claims.py" "${REACH_IMG}" \
        > "${CLAIMS_OUT}" 2>&1
    claims_rc=$?
    tail -1 "${CLAIMS_OUT}" | sed 's/^/[claims]   /' | tee -a "${LOG}"
    [ "${claims_rc}" != "0" ] && log "[claims] rc=${claims_rc} — a PDK the image ships is undeclared, or a declared one is gone"
else
    echo "MISSING: fork-gatekeeper/check_image_claims.py or no released image — nothing was checked" \
        > "${CLAIMS_OUT}"
    log "[claims] nothing was checked, which is not a clean result"
fi

# --- DID CI RUN AT ALL? (vibe-ic#550, extended to this repo 2026-07-29) ---
# #550 was filed against vibe-ic: Actions disabled at the ACCOUNT level, 561
# commits landed with no CI, and nothing noticed for nine days because
# `gh run list` prints nothing for "never ran" and nothing for "no match".
#
# Measured here, same account, same day:
#
#     vibeic/vibe-ic      2 runs   (both Dependency Graph, none CI)
#     vibeic/vibeic-eda   0 runs   3 workflows, all `active`
#     gh workflow run fork-only.yml -> HTTP 422:
#         "Actions has been disabled for this user."
#
# So this repo is under the same block, has three workflows that have never
# executed, and — unlike vibe-ic, which gained `ci_ran_at_all_check` in v1.8.2 —
# had NO gate that could notice. The fork-only workflow landed earlier today is
# in exactly that position: present, active, and never once run.
#
# The checker is REUSED from the plugin rather than reimplemented. A second
# implementation of "did CI run" is how two programs come to disagree about the
# same question, which is the defect vibeic-eda#29 was about.
#
# Non-fatal here BY DESIGN: the block is account-level and no tick can clear it,
# so failing the tick would turn a standing owner-action into daily noise. It is
# logged loudly, and rc 2 (could not look) stays distinct from rc 1 (no run).
# Defined BEFORE the branch that reads it. This script runs under `set -u`,
# and line 200 already records what that costs: a variable assigned only
# inside a conditional and read outside aborts the whole tick rather than
# skipping a step. Same shape, same file, so it gets the same treatment.
CI_OUT="${LOG_DIR}/ci-ran-at-all.txt"
CI_CHECK="${VIBE_IC_PROGRAMS:-/home/reyerchu/vibe-ic/vibe-ic-marketplace/plugins/vibe-ic/programs}/ci_ran_at_all_check.py"
if [ -f "${CI_CHECK}" ]; then
    python3 "${CI_CHECK}" "${DIR}/.." > "${CI_OUT}" 2>&1
    ci_rc=$?
    tail -1 "${CI_OUT}" | sed 's/^/[ci-ran]   /' | tee -a "${LOG}"
    [ "${ci_rc}" != "0" ] && log "[ci-ran] rc=${ci_rc} — see vibe-ic#550; the tick does not fail on it because no tick can re-enable Actions"
else
    echo "MISSING: ${CI_CHECK} — whether CI ran was NOT checked" > "${CI_OUT}"
    log "[ci-ran] nothing was checked, which is not a clean result"
fi

# vibeic-eda#35 — is the image the union of the forks' masters?
#
# `check_our_commits_ship.py` answers exactly the question that issue asks, it
# has been correct since it was written, and NOTHING CALLED IT. It reported
# nothing for as long as it existed; the 36-stranded state it was built to
# detect went unnoticed for precisely that reason. A check that exists, is
# right, and is never invoked is indistinguishable from one that was never
# written — the same "absence rendering as a pass" this tick keeps finding
# elsewhere.
#
# It compares by PATCH EQUIVALENCE (`git cherry`), not by SHA reachability: the
# same fix cherry-picked onto a dozen branches is one fix, and counting SHAs
# overcounted it 136-fold when this was first measured.
#
# FAILS the tick (rc 6), unlike the CI block above. The distinction is who can
# act: an account-level Actions block is a standing owner action no tick can
# clear, so failing on it would be daily noise. A commit of ours that no build
# branch reaches IS this tick's job — the merge step it runs is what puts it
# there — so a non-zero here means the round did not finish its own work.
SHIP_OUT="${LOG_DIR}/our-commits-ship.txt"
ship_rc=0
if [ -f "${DIR}/check_our_commits_ship.py" ]; then
    python3 "${DIR}/check_our_commits_ship.py" \
        --json "${LOG_DIR}/our-commits-ship.json" > "${SHIP_OUT}" 2>&1
    ship_rc=$?
    tail -1 "${SHIP_OUT}" | sed 's/^/[ship]   /' | tee -a "${LOG}"
    grep -E "STRANDED|NOT reachable" "${SHIP_OUT}" | sed 's/^/[ship]   /' | tee -a "${LOG}" || true
    [ "${ship_rc}" != "0" ] && log "[ship] rc=${ship_rc} — a commit of ours is not reachable from the branch its pin names (vibeic-eda#35)"
else
    echo "MISSING: ${DIR}/check_our_commits_ship.py — whether our commits ship was NOT checked" > "${SHIP_OUT}"
    log "[ship] nothing was checked, which is not a clean result"
    ship_rc=2
fi

# vibeic-eda#60 — the mirror image of the check above. `check_our_commits_ship`
# asks whether a commit of ours is reachable from the branch its PIN names;
# this asks whether a fork carrying commits of ours is pinned AT ALL.
#
# Latent today: the four unpinned forks (ciel, open_pdks, sv2v, IHP-Open-PDK)
# carry zero of our commits, so the condition is unreachable. It activates on
# the first patch, when the ledger reports ahead=1 on a fork the image does not
# build from — and a number going UP is the last place anyone looks for a
# failure. Added while it is free.
#
# Placed AFTER the block above, not inside the comment paragraph that describes
# it: the first attempt anchored on the earliest mention of
# `check_our_commits_ship.py` in this file, which is prose, and `bash -n`
# accepted the result.
UNSHIP_OUT="${LOG_DIR}/unshippable-patches.txt"
unship_rc=0
if [ -f "${DIR}/check_unshippable_patches.py" ]; then
    python3 "${DIR}/check_unshippable_patches.py" > "${UNSHIP_OUT}" 2>&1
    unship_rc=$?
    tail -1 "${UNSHIP_OUT}" | sed 's/^/[unship] /' | tee -a "${LOG}"
    [ "${unship_rc}" = "1" ] && log "[unship] a fork carries patches that cannot reach the image (vibeic-eda#60)"
else
    echo "MISSING: ${DIR}/check_unshippable_patches.py — nothing asked" > "${UNSHIP_OUT}"
    log "[unship] nothing was checked, which is not a clean result"
    unship_rc=2
fi

# vibeic-eda#93 — the patch->oracle map, asking BOTH of its questions.
#
# The hand-built map asked "does this commit SHIP a test?" and nothing else, so
# `ee778e7ced` (494 adds) was filed NO-ORACLE while it was in fact the CAUSE of
# a dead one: it changed the routed DEF that upstream's `drt:top_level_term2`
# diffs against, the golden was never regenerated, and the test has been
# permanently red since. Ship-a-test and break-a-test are opposite conditions
# with opposite remedies, and one question cannot separate them.
#
# DOES NOT FAIL THE TICK, and the reason is the same one that makes the CI block
# above non-fatal: who can act. A stale golden is cleared by a source change a
# human lands; no tick can regenerate it, so failing here would be daily noise
# on a standing state. What it must not do is stay invisible — hence the log line
# and the JSON, on every round.
#
# COULD-NOT-MEASURE is the expected majority answer and is NOT a defect of this
# check: the verdict ledgers cover the modules that were actually run. An absent
# verdict is reported as absent, never as a pass.
ORACLE_OUT="${LOG_DIR}/oracle-map.txt"
oracle_rc=0
if [ -f "${DIR}/oracle_map.py" ]; then
    OR_DIR="/home/reyerchu/vibe-ic-forks/OpenROAD"
    LEDGERS=""
    for L in "${DIR}"/ORACLE_VERDICTS.OpenROAD.*.json; do
        [ -f "${L}" ] && LEDGERS="${LEDGERS} --verdicts ${L}"
    done
    # shellcheck disable=SC2086
    python3 "${DIR}/oracle_map.py" --repo "${OR_DIR}" --head HEAD \
        --json "${LOG_DIR}/oracle-map.json" ${LEDGERS} > "${ORACLE_OUT}" 2>&1
    oracle_rc=$?
    grep -E "BROKE-EXISTING-ORACLE ·|counting:" "${ORACLE_OUT}" \
        | sed 's/^/[oracle] /' | tee -a "${LOG}" || true
    [ "${oracle_rc}" = "1" ] && log "[oracle] a commit of ours BROKE a pre-existing test — the remedy is to regenerate the golden, not to write a test (vibeic-eda#93)"
    [ "${oracle_rc}" = "2" ] && log "[oracle] the map could not be built — nothing was measured, which is not a clean result"
else
    echo "MISSING: ${DIR}/oracle_map.py — whether our commits broke an existing test was NOT asked" > "${ORACLE_OUT}"
    log "[oracle] nothing was checked, which is not a clean result"
    oracle_rc=2
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
# Nor by our own commits failing to reach the branches the image builds from.
[ "${ship_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=6
# Nor by a capability the base image gave us and ours no longer resolves
# (vibeic-eda#88). rc=2 from that program means it could not probe the image at
# all, and it lands here for the same reason rc=1 does: nothing was compared.
[ "${cap_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=7
log "[done] gatekeeper tick exit ${rc}"
exit ${rc}
