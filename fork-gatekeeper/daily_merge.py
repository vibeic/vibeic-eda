#!/usr/bin/env python3
"""Merge ALL new upstream commits into every fork we build. Every day. No PR.

OWNER RULING, 2026-07-29 (two statements, both binding):
  * "daily merge all new commit from upstream for forked tools"
  * "每天自動合併、不等人" — merge automatically, do not wait for a human.

This supersedes the selective-adoption path (`GK_MERGE_PR`), which opens a
cherry-pick PR for the commits an LLM judged clearly-safe and leaves everything
else for a person. That path is still correct for what it does and is not
removed; it is no longer the thing that decides whether we are current.

WHAT THIS DOES DIFFERENTLY
==========================
Selective adoption asks "should we take this commit?", one commit at a time,
and its default answer is "ask a human". Measured the day the ruling landed:
9 assessments existed, 5 commits had ever been auto-adopted, 35 were queued for
a human, and the fleet was 1229 commits behind — because the projects that
matter most (OpenROAD, yosys, verilator, iverilog, ngspice) do not tag releases
at all, so they had never even entered the queue.

This takes the whole upstream branch. `git merge`, not cherry-pick. The fork's
own commits are carried by the merge and verified afterwards.

WHAT IT REFUSES TO DO
=====================
A conflict is NOT resolved automatically. Merging everything is a policy the
owner set; guessing what a conflicting hunk should say is not, and a wrong
resolution compiles. On conflict this ABORTS the merge, leaves the fork
untouched, and reports the file list — that fork stays behind until a human
resolves it, and the report says so rather than reporting success.

It also never force-pushes, never touches a build branch it did not
fast-forward, and never runs in the shared checkout's working tree.

Exit: 0 every fork current or already current, 1 at least one needs a human,
      2 nothing was attempted (which is not success).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: Where the fork checkouts live. Each is a SHARED tree that other sessions and
#: this tick's siblings use, so every operation here runs in a worktree of it and
#: never in the tree itself.
FORKS_ROOT = Path(os.environ.get("VIBEIC_FORKS_ROOT", "/home/reyerchu/vibe-ic-forks"))


def _sh(args: List[str], cwd: Optional[Path] = None, timeout: int = 900):
    try:
        p = subprocess.run(args, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


#: vibeic repo -> upstream slug, for the sources that are mirrors rather than
#: forks. Kept small and explicit: every entry is a repo the image clones, and
#: adding one by hand is the price of the fork API refusing us.
MIRROR_UPSTREAMS = {
    "kissat": "arminbiere/kissat",
    "cadical": "arminbiere/cadical",
    "ASAP7_for_KLayout": "laurentc2/ASAP7_for_KLayout",
    "asap7_pdk_r1p7": "The-OpenROAD-Project/asap7_pdk_r1p7",
    "asap7sc7p5t_28": "The-OpenROAD-Project/asap7sc7p5t_28",
    "OpenROAD-flow-scripts": "The-OpenROAD-Project/OpenROAD-flow-scripts",
}


def _upstream_url(repo: str) -> str:
    """Upstream of a vibeic fork, asked of GitHub rather than hard-coded."""
    rc, out, _ = _sh(["gh", "api", f"repos/vibeic/{repo}",
                      "--jq", ".parent.full_name // empty"], timeout=120)
    slug = out.strip() if rc == 0 else ""
    # `gh api` prints the ERROR BODY to stdout on failure, and `--jq` leaves it
    # there when the document has no such field. Without the rc check a missing
    # repo produced
    #     https://github.com/{"message":"Not Found",...}.git
    # — a URL built out of an error message, which git would then fail to clone
    # for a reason with nothing to do with the actual problem. Found by probing
    # a repo that does not exist, which is the case nobody writes a test for.
    if not slug or "/" not in slug or slug.startswith("{"):
        slug = ""
    if not slug:
        # NINE sources are PUSH MIRRORS, not GitHub forks: the fork API answered
        # 403 for all of them, so they were created as repos and pushed. GitHub
        # records no `parent` for those, and a program that reads the parent
        # therefore sees nothing. The upstream is still a fact — it is just a
        # fact GitHub is not holding for us, so we hold it rather than reporting
        # the mirror as unsurveyable.
        #
        # FORKS.json FIRST, the hard-coded map second. vibeic-eda#30 mirrored
        # open_pdks, IHP-Open-PDK and ciel and declared all three there — and
        # all three still reported NO_UPSTREAM, because the map below was not
        # extended and nothing required it to be. A declaration that one program
        # reads and another ignores is how #30 landed without doing its job.
        # Reading the declaration means the next mirror works with no edit here.
        slug = _declared_upstream(repo) or MIRROR_UPSTREAMS.get(repo, "")
    return f"https://github.com/{slug}.git" if slug else ""


@functools.lru_cache(maxsize=1)
def _declared_upstreams() -> Dict[str, str]:
    """tool -> upstream slug, from FORKS.json. Empty when it cannot be read."""
    f = Path(__file__).resolve().parent / "FORKS.json"
    try:
        return {x["tool"]: x["upstream"] for x in
                json.loads(f.read_text())["forks"] if x.get("upstream")}
    except (OSError, ValueError, KeyError):
        return {}


def _declared_upstream(repo: str) -> str:
    return _declared_upstreams().get(repo, "")


def build_branches(eda_root: Path) -> Dict[str, str]:
    """fork repo -> the pinned commit, or "" for a source we track without a pin.

    Read rather than configured: a branch list is a second copy of a value the
    Dockerfiles already state, and the two drift the moment someone repoints a
    build without updating the list.

    A PIN IS NOT THE ONLY REASON TO WATCH A SOURCE, and assuming it was made
    vibeic-eda#30 land without doing its job. That change mirrored open_pdks,
    IHP-Open-PDK and ciel so the daily upstream check would watch them, and
    declared all three in FORKS.json — and this survey kept reporting 18 of 30,
    because it enumerates from `ARG <NAME>_REF=<40 hex>` and those three have no
    such ARG: the Dockerfile clones the PDKs by URL, and the PDKs themselves
    arrive in the base image.

    So the mirrors existed, the declaration existed, and nothing looked at them
    — the exact gap #30 was opened to close, surviving the change that closed
    it. FORKS.json is now the second source: a declared source with no pin is
    surveyed with pin "", and `branch_for` falls back to the fork's default
    branch. The upstream side is the value for those; there is no pin to report
    and that is correct rather than missing.
    """
    out: Dict[str, str] = {}
    for df in sorted((eda_root / "tools").glob("*/Dockerfile")) + [eda_root / "Dockerfile"]:
        if not df.is_file():
            continue
        text = df.read_text(errors="replace")
        for m in re.finditer(
                r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text):
            repo = m.group(1)
            arg = repo.upper().replace("-", "_")
            line = re.search(rf"^ARG {arg}_REF=([0-9a-f]{{40}}).*$", text, re.M)
            if line:
                out.setdefault(repo, line.group(1))

    declared = eda_root / "fork-gatekeeper" / "FORKS.json"
    if declared.is_file():
        try:
            for f in json.loads(declared.read_text())["forks"]:
                out.setdefault(f["tool"], "")
        except (ValueError, KeyError) as exc:                  # noqa: BLE001
            print(f"[warn] {declared} unreadable ({exc}) — surveying pinned "
                  f"sources only, which is fewer than we track", file=sys.stderr)
    return out


def branch_for(repo: str, pin: str) -> Optional[str]:
    """The branch whose tip IS the pin — asked of the fork, not inferred.

    An earlier version read `branch <name>` out of the pin's trailing comment.
    That comment is not load-bearing and does not survive: `_bump_pin` rewrites
    the pin with `sed` and the comment goes with it, which is exactly what
    happened to yosys and ngspice in the 2026-07-29 merge. The parser then
    reported 12 forks out of 14 and said nothing about the two it lost — a
    fleet-wide merger that silently skips two tools is worse than one that
    refuses to start.

    The fork itself knows. Ask it.

    NO PIN -> the fork's default branch. A source we track without pinning (the
    three PDK mirrors, and the data mirrors the build clones by URL) still has
    an upstream worth merging from; it just has no commit of ours to locate.
    Returning None for these is what kept them out of the survey entirely.
    """
    if not pin:
        rc, out, _ = _sh(["gh", "api", f"repos/vibeic/{repo}",
                          "--jq", ".default_branch"], timeout=120)
        return out.strip() if rc == 0 and out.strip() else None
    # --paginate + per_page: the unpaginated endpoint returns the FIRST 30
    # branches. Measured: that made four forks report NO_BRANCH_AT_PIN whose pin
    # was in fact the exact tip of their build branch — a truncated list that
    # reads as a missing branch, the same shape as vibeic-eda#15's fork count.
    rc, out, _ = _sh(["gh", "api", "--paginate",
                      f"repos/vibeic/{repo}/branches?per_page=100",
                      "--jq", ".[] | .name + \" \" + .commit.sha"], timeout=180)
    if rc != 0:
        return None
    hits = [ln.split()[0] for ln in out.splitlines()
            if len(ln.split()) == 2 and ln.split()[1] == pin]
    if not hits:
        return None
    # SEVERAL branches can sit at one commit — a merge branch pushed today, a
    # temporary fix branch, and the build branch all at the same tip. Picking the
    # first is picking by luck of API ordering, and it picked
    # `vibeic/daily-merge-2026-07-29` over the build branch on the first run.
    # Prefer a durable name: not dated, not obviously temporary.
    def durable(b: str) -> tuple:
        return (bool(re.search(r"\d{4}-\d{2}-\d{2}", b)),      # dated -> last
                bool(re.search(r"\b(fix|tmp|temp|wip|test)\b", b, re.I)),
                # `integration` is this fleet's build-branch convention
                # (openroad-integration, satfix-integration, klayout-signoff-int,
                # batch-honesty-integration). Preferring the SHORTER name picked
                # today's throwaway `vibeic/fin-bazel-fix` over
                # `vibeic/openroad-integration` — same commit, so delivery was
                # unaffected, but merging into it would have split the fork.
                not re.search(r"int(egration)?$", b, re.I),
                len(b))
    return sorted(hits, key=durable)[0]


def merge_one(repo: str, branch: str, dry: bool = False) -> dict:
    """Merge upstream's default branch into `branch`, in an isolated worktree."""
    src = FORKS_ROOT / repo
    res = {"repo": repo, "branch": branch, "state": "?", "detail": "",
           "conflicts": [], "took": 0}
    if not (src / ".git").exists() and not (src / "HEAD").exists():
        # A fork with no local checkout is not a fork with nothing to merge.
        # Newly-created mirrors land here on their first tick; clone rather than
        # report a state that reads like "nothing to do".
        src.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = _sh(["git", "clone", "--quiet",
                          f"https://github.com/vibeic/{repo}.git", str(src)],
                         timeout=1800)
        if rc != 0:
            res.update(state="CLONE_FAILED", detail=err.strip()[:200])
            return res
        _sh(["git", "-C", str(src), "remote", "add", "upstream",
             _upstream_url(repo)])

    # THE SHARED TREE IS READ-ONLY TO US. Its working tree may hold someone
    # else's uncommitted work — measured: the OpenROAD checkout carried five
    # uncommitted files belonging to another session on the day this was written.
    before = _sh(["git", "status", "--porcelain"], src)[1]

    rc, out, err = _sh(["git", "-C", str(src), "remote", "get-url", "upstream"])
    if rc != 0 or not out.strip():
        # A freshly-mirrored fork has no `upstream` remote. Configure it from
        # what GitHub says the parent is, rather than refusing: "no upstream
        # configured" is a fact about our clone, not about the fork.
        url = _upstream_url(repo)
        if not url:
            res.update(state="NO_UPSTREAM",
                       detail="GitHub records no parent for this repo — a push "
                              "mirror, not a fork; upstream must be set by hand")
            return res
        # `add` FAILS when the remote exists with an empty URL, which is what a
        # mirror's checkout looks like — and the failure surfaced one step later
        # as `git fetch upstream` -> "fatal: no path specified", a message about
        # a URL rather than about the missing parent it actually was.
        rc_add, _, _ = _sh(["git", "-C", str(src), "remote", "add",
                            "upstream", url])
        if rc_add != 0:
            _sh(["git", "-C", str(src), "remote", "set-url", "upstream", url])
        out = url
    parent = out.strip()

    tmp = Path(tempfile.mkdtemp(prefix=f"daily_merge_{repo}_"))
    wt = tmp / "wt"
    try:
        # origin FIRST. The build branch is resolved from the fork over the API,
        # so a stale remote-tracking ref makes `origin/<branch>` an invalid
        # reference for a branch that plainly exists — measured on the first
        # dry-run, which reported WORKTREE_FAILED for six forks that were fine.
        # WIDEN THE REFSPEC FIRST. Six of these checkouts were cloned
        # `--single-branch`, so `remote.origin.fetch` names ONE branch and
        # `git fetch origin` never brings any other. `origin/<build-branch>` is
        # then an invalid reference for a branch that plainly exists on the fork
        # — which is what six WORKTREE_FAILED rows were, and reading them as
        # "the branch is missing" would have been exactly backwards.
        #
        # It also makes local counts lie: OpenROAD kept a stale
        # `origin/vibeic/openroad-integration` from an older clone that fetch
        # never updated, and measuring against it reported 791 commits behind
        # upstream for a branch that had already been merged.
        _sh(["git", "-C", str(src), "remote", "set-branches", "origin", "*"])
        _sh(["git", "-C", str(src), "fetch", "origin", "--prune"], timeout=1800)
        rc, _, err = _sh(["git", "-C", str(src), "fetch", "upstream", "--prune"],
                         timeout=1800)
        if rc != 0:
            res.update(state="FETCH_FAILED", detail=err.strip()[:200])
            return res

        # The upstream default branch, asked rather than assumed. Defaulting to
        # `master` made sby and ALIGN-pdk-sky130 report COUNT_FAILED against a
        # branch upstream does not have — the count was fine, the branch name
        # was invented.
        rc, out, _ = _sh(["git", "-C", str(src), "symbolic-ref",
                          "refs/remotes/upstream/HEAD"])
        up = out.strip().split("/")[-1] if rc == 0 and out.strip() else ""
        if not up:
            slug = parent.rstrip("/").removesuffix(".git").split("github.com/")[-1]
            _, api, _ = _sh(["gh", "api", f"repos/{slug}",
                             "--jq", ".default_branch // empty"], timeout=120)
            up = api.strip()
        if not up or _sh(["git", "-C", str(src), "rev-parse", "--verify",
                          f"upstream/{up}"])[0] != 0:
            for cand in ("master", "main", "develop"):
                if _sh(["git", "-C", str(src), "rev-parse", "--verify",
                        f"upstream/{cand}"])[0] == 0:
                    up = cand
                    break

        rc, _, err = _sh(["git", "-C", str(src), "worktree", "add", "--detach",
                          str(wt), f"origin/{branch}"], timeout=1800)
        if rc != 0:
            res.update(state="WORKTREE_FAILED", detail=err.strip()[:200])
            return res

        # A SHALLOW clone cannot be counted and must not be merged. The shared
        # OpenROAD checkout is grafted at one commit, and `rev-list --count` over
        # it reported 42508 commits behind where GitHub's compare says 19. A
        # merge computed against a graft is worse than a wrong number: it has no
        # true merge base. Deepen first; the cost is once per clone.
        rc, sh_out, _ = _sh(["git", "-C", str(src), "rev-parse",
                             "--is-shallow-repository"])
        if sh_out.strip() == "true":
            rc, _, err = _sh(["git", "-C", str(src), "fetch", "--unshallow",
                              "origin"], timeout=3600)
            if rc != 0:
                _sh(["git", "-C", str(src), "fetch", "--depth=2147483647",
                     "origin"], timeout=3600)
            _sh(["git", "-C", str(src), "fetch", "upstream", "--prune"],
                timeout=3600)
            rc, sh_out, _ = _sh(["git", "-C", str(src), "rev-parse",
                                 "--is-shallow-repository"])
            if sh_out.strip() == "true":
                res.update(state="SHALLOW_NEEDS_HUMAN",
                           detail="clone is shallow and could not be deepened; "
                                  "any count or merge over it is meaningless")
                return res

        rc, out, _ = _sh(["git", "rev-list", "--count",
                          f"HEAD..upstream/{up}"], wt)
        if rc != 0 or not out.strip().isdigit():
            # My first version stored -1 here and printed `WOULD_MERGE -1
            # commit(s)`. That reads as a merge decision when it is a FAILED
            # MEASUREMENT, which is the exact confusion this program exists to
            # stop. A count we could not take is a state of its own.
            res.update(state="COUNT_FAILED",
                       detail=f"could not count HEAD..upstream/{up} — the "
                              f"branch may not exist on upstream")
            return res
        behind = int(out.strip())
        res["took"] = behind
        if behind == 0:
            res.update(state="ALREADY_CURRENT", detail=f"level with upstream/{up}")
            return res
        if dry:
            res.update(state="WOULD_MERGE", detail=f"{behind} commit(s) behind upstream/{up}")
            return res

        rc, _, err = _sh(["git", "-c", "user.name=vibeic-fork-gatekeeper",
                          "-c", "user.email=fork-gatekeeper@vibeic.ai",
                          "merge", f"upstream/{up}", "-m",
                          f"Merge upstream/{up} into {branch} (daily, owner ruling 2026-07-29)"],
                         wt, timeout=1800)
        if rc != 0:
            _, cf, _ = _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt)
            res["conflicts"] = [c for c in cf.splitlines() if c.strip()]
            # ABORT, do not guess. Merging everything is the owner's policy;
            # resolving a conflicting hunk is a judgement, and a wrong one
            # compiles.
            _sh(["git", "merge", "--abort"], wt)
            res.update(state="CONFLICT_NEEDS_HUMAN",
                       detail=f"{len(res['conflicts'])} file(s) conflicted; merge aborted, "
                              f"fork left at its previous tip")
            return res

        # Our own commits must still be REACHABLE. A merge that exits 0 having
        # dropped them is the failure this check exists for.
        rc, out, _ = _sh(["git", "rev-list", "--count",
                          f"upstream/{up}..HEAD"], wt)
        ours = int(out.strip() or 0) if rc == 0 else -1

        rc, _, err = _sh(["git", "push", "origin", f"HEAD:{branch}"], wt, timeout=1800)
        if rc != 0:
            res.update(state="PUSH_FAILED", detail=err.strip()[:200])
            return res
        res.update(state="MERGED",
                   detail=f"took {behind} upstream commit(s); {ours} of ours still ahead")
        return res
    finally:
        _sh(["git", "-C", str(src), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(tmp, ignore_errors=True)
        after = _sh(["git", "status", "--porcelain"], src)[1]
        if after != before:
            res["detail"] += ("  !! the shared checkout's working tree CHANGED "
                              "during this merge — investigate")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--only", default=None, help="one fork, for testing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    pins = build_branches(Path(a.eda_root))
    if a.only:
        pins = {k: v for k, v in pins.items() if k == a.only}
    branches, unresolved = {}, []
    for repo, pin in sorted(pins.items()):
        b = branch_for(repo, pin)
        (branches.__setitem__(repo, b) if b else unresolved.append((repo, pin)))
    for repo, pin in unresolved:
        # NAMED, not dropped. A fork whose pin matches no branch tip is a real
        # finding — the pin names a commit no branch carries — and it must not
        # look like a fork with nothing to merge.
        print(f"  {repo:<14} NO_BRANCH_AT_PIN      pin {pin[:9]} is not the tip "
              f"of any branch on the fork", file=sys.stderr)
    if not branches:
        print("[NOT CHECKED] no fork/branch pairs found in the Dockerfiles — "
              "nothing was attempted, which is not success", file=sys.stderr)
        return RC_NOTHING

    results = [merge_one(r, b, a.dry_run) for r, b in sorted(branches.items())]
    merged = [r for r in results if r["state"] == "MERGED"]
    human = [r for r in results if r["state"] not in ("MERGED", "ALREADY_CURRENT",
                                                      "WOULD_MERGE")]

    print(f"daily_merge: {len(results)} fork(s), {len(merged)} merged, "
          f"{sum(r['took'] for r in merged)} upstream commit(s) taken, "
          f"{len(human)} need a human")
    for r in results:
        print(f"  {r['repo']:<14} {r['state']:<22} {r['detail'][:80]}")
        for c in r["conflicts"][:10]:
            print(f"      conflict: {c}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "daily_merge", "forks": results}, indent=2) + "\n",
            encoding="utf-8")

    if human:
        print(f"[NEEDS HUMAN] {len(human)} fork(s) did not merge cleanly; each is "
              f"left at its previous tip", file=sys.stderr)
        return RC_NEEDS_HUMAN
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
