#!/usr/bin/env bash
# build_and_regress.sh — the option-B image_build.cmd (owner-approved).
#
# For each candidate tool (a fork with a NEW upstream release), try to integrate it:
#   1. rebase our vibeic/* branch onto the new release tag (in a scratch worktree).
#      - tag missing        -> status "tag_missing"  (defer)
#      - rebase CONFLICT     -> abort, status "rebase_conflict" + the file (defer; our
#                               patch overlaps the upstream change — needs human review)
#      - clean               -> status "rebased_clean", new sha recorded
#   2. for the clean set: temp-push the rebased sha (so the build can fetch it), bump the
#      vibeic-eda Dockerfile ARG(s) in a SCRATCH build tree, `docker build` the image,
#      and run a smoke regression against it.
#      - build/regress green -> status "built_green"
#      - red                 -> status "built_red"   (defer)
#   3. PROMOTE only when GK_MODE=promote AND green: fast-forward the real vibeic branch,
#      push it, commit the Dockerfile+VERSION bump, and push the new image (release.sh).
#      Default GK_MODE=verify → build is proven but NOTHING in production is changed
#      (temp refs + scratch tree cleaned; no branch move, no image push). This is the
#      owner's staged rollout ("verify the build passes first, then enable auto-push").
#
# Input : env VIBEIC_CANDIDATES = JSON [{tool,arg,branch,release}]  (set by gatekeeper.py)
# Output: writes per-candidate results JSON to $GK_RESULT (default result.json), which
#         the gatekeeper reads to set each tool's verdict.
set -uo pipefail

FORKS_DIR="${GK_FORKS_DIR:-/home/reyerchu/vibe-ic-forks}"
EDA_CLONE="${GK_EDA_CLONE:-/home/reyerchu/vibeic-eda}"
GK_MODE="${GK_MODE:-verify}"                    # verify | promote
GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
GK_RESULT="${GK_RESULT:-${GK_STATE_DIR}/last_build_result.json}"
STAMP="$(date +%Y%m%d)"
SCRATCH="/tmp/gk-build-${STAMP}"
IMG="vibeic/vibeic-eda:gkcandidate-${STAMP}"
RESULTS="[]"

emit() { python3 - "$RESULTS" "$1" "$2" "$3" "$4" <<'PY'
import json,sys
arr=json.loads(sys.argv[1]); arr.append({"tool":sys.argv[2],"status":sys.argv[3],"detail":sys.argv[4][:300],"sha":sys.argv[5] if len(sys.argv)>5 else ""})
print(json.dumps(arr))
PY
}

# a lenient "the tool actually runs" check inside the freshly built image, so a
# promoted tool is exercised (not just the yosys/openroad baseline). Presence fallback
# for tools without a stable version flag — build success is the strong signal.
_tool_smoke() {
    case "$1" in
        yosys)           echo "yosys -V" ;;
        OpenROAD)        echo "openroad -version" ;;
        klayout)         echo "klayout -v" ;;
        magic)           echo "magic --version || command -v magic" ;;
        netgen)          echo "command -v netgen" ;;
        iverilog)        echo "iverilog -V >/dev/null" ;;
        ngspice)         echo "ngspice --version >/dev/null 2>&1 || command -v ngspice" ;;
        sby)             echo "sby --help >/dev/null 2>&1 || command -v sby" ;;
        cocotb)          echo "python3 -c 'import cocotb'" ;;
        cocotb-coverage) echo "python3 -c 'import cocotb_coverage'" ;;
        pyuvm)           echo "python3 -c 'import pyuvm'" ;;
        *)               echo "command -v $1" ;;
    esac
}

# mark every clean candidate promote_failed (→ the gatekeeper maps that to DEFERRED with
# the real reason) WITHOUT touching the working tree. Used by the step-0 PRESERVATION
# guards (dirty tree / ahead-of-origin / fetch fail) — those must NEVER discard human
# work. Mutates the global RESULTS, so it must run in the caller's shell (no subshell).
_pf_mark() {
    local tool
    for tool in "${!CLEAN_SHA[@]}"; do
        RESULTS="$(emit "$tool" "promote_failed" "$1" "${CLEAN_SHA[$tool]}")"
    done
    echo "[promote] ABORT — $1 (tools stay DEFERRED)"
}
# post-mutation abort: undo OUR OWN edits (reset to the already-reconciled origin/main),
# then mark. Only safe AFTER step 0 verified the tree is clean + not ahead of origin, so
# the reset discards nothing but this run's sed/version-bump. NEVER call from a step-0
# guard — that would wipe the human WIP the guard exists to protect.
_pf_abort() {
    git -C "${EDA_CLONE}" rebase --abort 2>/dev/null
    git -C "${EDA_CLONE}" reset --hard origin/main -q 2>/dev/null
    _pf_mark "$1"
}

# GK_MODE=promote: ship the verified-green image + integrate the fork branches for real.
# CORRECT-BY-ORDER + fail-loud (rewritten after an adversarial review found 8 HIGH bugs
# in a swallow-and-continue version):
#   0. reconcile the vibeic-eda clone to origin/main — so NEWVER is computed from ORIGIN
#      (a stuck later step re-ships the SAME version, never inflates) and the commit
#      fast-forwards. Refuse on a dirty tree or unpushed local commits (human WIP).
#   1. GATE fork branches FIRST: land each rebased sha on its durable vibeic branch before
#      anything pins it; any push failure → abort, ship NOTHING.
#   2. bump Dockerfile pins + VERSION (tolerate sync's repo-wide drift audit — verify the
#      write took, don't gate on unrelated stale pointers).
#   3. GATE the image ship: :NEWVER then :latest, both checked + timeout-bounded. Shipped
#      BEFORE the origin commit so a failed origin push re-ships the same version (converges).
#   4. GATE the origin commit+push (retry once on non-ff via rebase); on failure roll the
#      LOCAL commit back → next tick retries.
#   5. only NOW emit "promoted" (→ MERGED). Every failure path emits "promote_failed"
#      (→ DEFERRED) and leaves a CLEAN tree, so nothing is falsely MERGED and the dirty-tree
#      guard can never wedge future runs. Docker Hub stays best-effort (GHCR is canonical).
# Mutates global RESULTS (call NOT in a subshell). Returns 0 only on a fully-landed promote.
promote_all() {
    local plog="/tmp/gk-push-${STAMP}.log" tool arg sha br fr CURVER NEWVER tools_csv DH="dockerhub:skipped"
    local T="${GK_PUSH_TIMEOUT:-1800}"
    # 0. reconcile — preservation guards use _pf_mark (NO reset): they exist to protect
    #    human WIP, so they must not touch the tree. The reconcile reset runs only AFTER
    #    they pass (tree clean + not ahead), so it discards nothing human.
    if [ -n "$(git -C "${EDA_CLONE}" status --porcelain 2>/dev/null)" ]; then
        _pf_mark "vibeic-eda tree is dirty — refusing to auto-commit (human attention)"; return 1
    fi
    if ! git -C "${EDA_CLONE}" fetch origin main -q 2>>"${plog}"; then
        _pf_mark "vibeic-eda: git fetch origin failed"; return 1
    fi
    if [ "$(git -C "${EDA_CLONE}" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)" != "0" ]; then
        _pf_mark "vibeic-eda main is ahead of origin (unpushed local commits) — refusing to reset"; return 1
    fi
    git -C "${EDA_CLONE}" reset --hard origin/main -q
    # 1. GATE fork branches ---------------------------------------------------
    for tool in "${!CLEAN_SHA[@]}"; do
        br="${CLEAN_BRANCH[$tool]}"; sha="${CLEAN_SHA[$tool]}"; fr="${CLEAN_FORKREMOTE[$tool]}"
        if [ -z "$br" ]; then _pf_abort "${tool}: no vibeic branch resolved — cannot integrate"; return 1; fi
        if ! git -C "${FORKS_DIR}/${tool}" push -f "${fr}" "${sha}:refs/heads/${br}" -q 2>>"${plog}"; then
            _pf_abort "${tool}: fork branch push (${br}) failed — nothing shipped"; return 1
        fi
        echo "[promote] ${tool}: fork ${br} -> ${sha:0:12}"
    done
    # 2. bump pins + VERSION (compute NEWVER from origin; tolerate unrelated drift) -------
    for tool in "${!CLEAN_SHA[@]}"; do
        arg="${CLEAN_ARG[$tool]}"; sha="${CLEAN_SHA[$tool]}"
        [ -n "$arg" ] && sed -i -E "s/^(ARG ${arg}=)[^[:space:]]+/\1${sha}/" "${EDA_CLONE}/Dockerfile"
    done
    CURVER="$(cd "${EDA_CLONE}" && ./sync_image_version.py --print 2>/dev/null)"
    # match sync_image_version.next_version()'s canonical scheme (patch 0..99, rollover to minor)
    NEWVER="$(python3 -c "x,y,z='${CURVER}'.split('.'); z=int(z); print(f'{x}.{int(y)+1}.0' if z>=99 else f'{x}.{y}.{z+1}')" 2>/dev/null)"
    if [ -z "${NEWVER}" ]; then _pf_abort "could not compute next version from '${CURVER}'"; return 1; fi
    ( cd "${EDA_CLONE}" && ./sync_image_version.py --set "${NEWVER}" ) >>"${plog}" 2>&1 \
        || echo "[promote] note: sync_image_version --set exit!=0 (likely unrelated repo drift); verifying the write"
    if [ "$(cd "${EDA_CLONE}" && ./sync_image_version.py --print 2>/dev/null)" != "${NEWVER}" ]; then
        _pf_abort "VERSION write to ${NEWVER} did not take"; return 1
    fi
    # 3. GATE the image ship (both tags, timeout-bounded) ---------------------
    docker tag "${IMG}" "ghcr.io/vibeic/vibeic-eda:${NEWVER}"
    docker tag "${IMG}" "ghcr.io/vibeic/vibeic-eda:latest"
    if ! timeout "${T}" docker push "ghcr.io/vibeic/vibeic-eda:${NEWVER}" >>"${plog}" 2>&1; then
        _pf_abort "GHCR push ${NEWVER} failed/timed out (see ${plog})"; return 1
    fi
    if ! timeout "${T}" docker push "ghcr.io/vibeic/vibeic-eda:latest" >>"${plog}" 2>&1; then
        timeout "${T}" docker push "ghcr.io/vibeic/vibeic-eda:latest" >>"${plog}" 2>&1 \
            || { _pf_abort "GHCR :latest push failed (:${NEWVER} shipped, :latest stale) — retry next tick"; return 1; }
    fi
    if timeout "${T}" bash -c "docker tag '${IMG}' 'vibeic/vibeic-eda:${NEWVER}' && docker push 'vibeic/vibeic-eda:${NEWVER}'" >>"${plog}" 2>&1; then
        timeout "${T}" bash -c "docker tag '${IMG}' 'vibeic/vibeic-eda:latest' && docker push 'vibeic/vibeic-eda:latest'" >>"${plog}" 2>&1 || true
        DH="dockerhub:${NEWVER}"
    else
        DH="dockerhub:skipped(login?)"
    fi
    # 4. GATE origin commit + push (retry once on non-ff) ---------------------
    tools_csv="$(IFS=,; echo "${!CLEAN_SHA[*]}")"
    ( cd "${EDA_CLONE}" && git add -A \
      && git commit -q -m "${NEWVER} — auto: rebased ${tools_csv} onto upstream release(s) [fork-gatekeeper]" ) \
      || { _pf_abort "vibeic-eda commit failed (image ${NEWVER} shipped; retry next tick)"; return 1; }
    if ! git -C "${EDA_CLONE}" push -q origin HEAD:main 2>>"${plog}"; then
        git -C "${EDA_CLONE}" fetch origin main -q 2>>"${plog}"
        if ! { git -C "${EDA_CLONE}" rebase origin/main -q >>"${plog}" 2>&1 \
               && git -C "${EDA_CLONE}" push -q origin HEAD:main 2>>"${plog}"; }; then
            _pf_abort "vibeic-eda origin push failed (image ${NEWVER} shipped; re-ships same ver next tick)"; return 1
        fi
    fi
    # 5. SUCCESS — everything landed -----------------------------------------
    for tool in "${!CLEAN_SHA[@]}"; do
        RESULTS="$(emit "$tool" "promoted" "shipped ghcr.io/vibeic/vibeic-eda:${NEWVER} (${DH}); ${CLEAN_BRANCH[$tool]} rebased" "${CLEAN_SHA[$tool]}")"
    done
    echo "[promote] DONE -> vibeic-eda:${NEWVER}"
    return 0
}

CANDS="$(python3 -c 'import json,os;print("\n".join("%s\t%s\t%s\t%s\t%s"%(c["tool"],c.get("arg") or "",c.get("branch") or "",c.get("release") or "",c.get("upstream") or "") for c in json.loads(os.environ.get("VIBEIC_CANDIDATES","[]"))))')"
[ -z "${CANDS}" ] && { echo "[]" > "${GK_RESULT}"; echo "no candidates"; exit 0; }

declare -A CLEAN_SHA CLEAN_ARG CLEAN_FORKREMOTE CLEAN_BRANCH
CLEAN_COUNT=0
while IFS=$'\t' read -r tool arg branch release upstream; do
    [ -z "$tool" ] && continue
    dir="${FORKS_DIR}/${tool}"
    if [ ! -d "${dir}/.git" ]; then RESULTS="$(emit "$tool" "no_clone" "no fork clone at ${dir}" "")"; continue; fi
    # Remote conventions vary per clone (yosys: origin=upstream; magic: origin=fork,
    # no upstream). Resolve BOTH by URL — fetch release tags from UPSTREAM, push the
    # rebased candidate to the FORK — adding a remote if the clone lacks one.
    up_url="https://github.com/${upstream}.git"
    fork_url="https://github.com/vibeic/${tool}.git"
    up_remote="$(git -C "$dir" remote -v | awk -v u="$up_url" '$2==u && /\(fetch\)/{print $1; exit}')"
    [ -z "$up_remote" ] && { git -C "$dir" remote remove gk_up 2>/dev/null; git -C "$dir" remote add gk_up "$up_url"; up_remote=gk_up; }
    fork_remote="$(git -C "$dir" remote -v | awk -v f="$fork_url" '$2==f && /\(fetch\)/{print $1; exit}')"
    [ -z "$fork_remote" ] && { git -C "$dir" remote remove gk_fork 2>/dev/null; git -C "$dir" remote add gk_fork "$fork_url"; fork_remote=gk_fork; }
    git -C "$dir" fetch "$up_remote" --tags -q 2>/dev/null
    git -C "$dir" fetch "$fork_remote" -q 2>/dev/null
    if ! git -C "$dir" rev-parse "refs/tags/${release}" >/dev/null 2>&1; then
        RESULTS="$(emit "$tool" "tag_missing" "no git tag ${release} on ${upstream} (checked remote ${up_remote})" "")"
        continue
    fi
    if [ -z "$branch" ]; then
        RESULTS="$(emit "$tool" "no_vibeic_branch" "no branch pin on ARG in vibeic-eda/Dockerfile — add a '# … branch <name>' comment to auto-integrate ${tool}" "")"
        continue
    fi
    # resolve the vibeic branch: prefer the fork remote-tracking ref, else a local branch
    br_ref="$branch"
    git -C "$dir" rev-parse --verify "refs/remotes/${fork_remote}/${branch}" >/dev/null 2>&1 && br_ref="refs/remotes/${fork_remote}/${branch}"
    wt="/tmp/gk-wt-${tool}"
    git -C "$dir" worktree remove --force "$wt" 2>/dev/null; rm -rf "$wt"
    if ! git -C "$dir" worktree add -q --detach "$wt" "$br_ref" 2>/tmp/gkwt; then
        RESULTS="$(emit "$tool" "worktree_fail" "branch ${branch}: $(tail -1 /tmp/gkwt)" "")"; continue
    fi
    if git -C "$wt" rebase "$release" >/tmp/gkrb 2>&1; then
        sha="$(git -C "$wt" rev-parse HEAD)"
        CLEAN_SHA[$tool]="$sha"; CLEAN_ARG[$tool]="$arg"; CLEAN_FORKREMOTE[$tool]="$fork_remote"; CLEAN_BRANCH[$tool]="$branch"; CLEAN_COUNT=$((CLEAN_COUNT+1))
        RESULTS="$(emit "$tool" "rebased_clean" "rebased ${branch} onto ${release}" "$sha")"
        # temp-push to the FORK so the docker build can fetch the rebased sha
        git -C "$wt" push -f "$fork_remote" "HEAD:refs/heads/gk-candidate/${STAMP}" -q 2>/dev/null \
            || echo "  warn: temp-push failed for ${tool} (build may not find sha)"
    else
        cf="$(grep -m1 -iE 'CONFLICT' /tmp/gkrb | sed 's/^/ /')"
        git -C "$wt" rebase --abort 2>/dev/null
        RESULTS="$(emit "$tool" "rebase_conflict" "our ${branch} patch overlaps upstream ${release}:${cf}" "")"
    fi
    git -C "$dir" worktree remove --force "$wt" 2>/dev/null; rm -rf "$wt"
done <<< "${CANDS}"

# ---- build the image with the clean candidates bumped in -------------------
if [ "${CLEAN_COUNT}" -gt 0 ]; then
    rm -rf "${SCRATCH}"; cp -a "${EDA_CLONE}" "${SCRATCH}"
    for tool in "${!CLEAN_SHA[@]}"; do
        arg="${CLEAN_ARG[$tool]}"; sha="${CLEAN_SHA[$tool]}"
        [ -n "$arg" ] && sed -i -E "s/^(ARG ${arg}=).*/\1${sha}/" "${SCRATCH}/Dockerfile"
    done
    echo "[build] docker build ${IMG} with: ${!CLEAN_SHA[*]}"
    if docker build -t "${IMG}" "${SCRATCH}" >"/tmp/gk-docker-${STAMP}.log" 2>&1; then
        # smoke regression: yosys/openroad baseline + a per-candidate-tool check so the
        # tool being shipped is actually exercised in the fresh image, not just assumed.
        SMOKE="yosys -V && openroad -version"
        for tool in "${!CLEAN_SHA[@]}"; do SMOKE="${SMOKE} && { $(_tool_smoke "$tool") ; }"; done
        SMOKE="${SMOKE} && echo SMOKE_OK"
        if docker run --rm --entrypoint bash "${IMG}" -lc "${SMOKE}" >/tmp/gksmoke 2>&1; then
            for tool in "${!CLEAN_SHA[@]}"; do
                RESULTS="$(emit "$tool" "built_green" "image ${IMG} built + smoke-passed" "${CLEAN_SHA[$tool]}")"
            done
            if [ "${GK_MODE}" = "promote" ]; then
                promote_all || echo "[promote] not completed — candidates stay DEFERRED (verified green, not shipped)"
            fi
        else
            for tool in "${!CLEAN_SHA[@]}"; do RESULTS="$(emit "$tool" "built_red" "smoke regression failed: $(tail -1 /tmp/gksmoke)" "")"; done
        fi
    else
        for tool in "${!CLEAN_SHA[@]}"; do RESULTS="$(emit "$tool" "built_red" "docker build failed (see /tmp/gk-docker-${STAMP}.log)" "")"; done
    fi
    rm -rf "${SCRATCH}"
    # cleanup temp candidate refs
    for tool in "${!CLEAN_SHA[@]}"; do git -C "${FORKS_DIR}/${tool}" push "${CLEAN_FORKREMOTE[$tool]}" --delete "gk-candidate/${STAMP}" -q 2>/dev/null; done
fi

echo "${RESULTS}" > "${GK_RESULT}"
echo "wrote results -> ${GK_RESULT}"
python3 -c 'import json,os;[print("  %-16s %-16s %s"%(r["tool"],r["status"],r["detail"][:70])) for r in json.load(open(os.environ["GK_RESULT"]))]' 2>/dev/null
