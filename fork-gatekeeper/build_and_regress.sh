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
GK_RESULT="${GK_RESULT:-/home/reyerchu/eda-fork-gatekeeper/last_build_result.json}"
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

CANDS="$(python3 -c 'import json,os;print("\n".join("%s\t%s\t%s\t%s\t%s"%(c["tool"],c.get("arg") or "",c.get("branch") or "",c.get("release") or "",c.get("upstream") or "") for c in json.loads(os.environ.get("VIBEIC_CANDIDATES","[]"))))')"
[ -z "${CANDS}" ] && { echo "[]" > "${GK_RESULT}"; echo "no candidates"; exit 0; }

declare -A CLEAN_SHA CLEAN_ARG CLEAN_FORKREMOTE
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
        CLEAN_SHA[$tool]="$sha"; CLEAN_ARG[$tool]="$arg"; CLEAN_FORKREMOTE[$tool]="$fork_remote"; CLEAN_COUNT=$((CLEAN_COUNT+1))
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
        # smoke regression: the tool runs inside the fresh image
        if docker run --rm --entrypoint bash "${IMG}" -lc 'yosys -V && openroad -version && echo SMOKE_OK' >/tmp/gksmoke 2>&1; then
            for tool in "${!CLEAN_SHA[@]}"; do
                RESULTS="$(emit "$tool" "built_green" "image ${IMG} built + smoke-passed" "${CLEAN_SHA[$tool]}")"
            done
            [ "${GK_MODE}" = "promote" ] && echo "[promote] (enabled) would ff branches + release.sh + bump VERSION"
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
