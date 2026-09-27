#!/usr/bin/env python3
"""Every commit WE wrote must be on the branch the image actually ships.

WHY THIS EXISTS (measured 2026-07-31)
=====================================
Twelve of our own commits sat on fork branches the image does not build from,
for up to twenty days, and nothing reported it:

    OpenROAD  11 commits  on `vibeic/post-route-detailed-routing-repair-int`
                          while the pin names `vibeic/openroad-integration`
    yosys      1 commit   on `vibeic/synth-fixes`
                          while the pin names `satfix-integration`

They were not "not yet rebuilt". They were NOT ON THE SHIPPED LINE AT ALL:
0 of 11 were reachable from the pinned branch. Among them were EM current-density
signoff, vectored dynamic IR, transient IR-drop, an OpenRCX process-file
converter, and two post-route min-area repairs — one of which fixes a sky130
regression.

WHY THE EXISTING MECHANISMS COULD NOT SEE IT
--------------------------------------------
`daily_merge.py` brings upstream INTO our forks — it never asks which of our
branches the image builds from. `daily_release.py` moves each pin to "its fork's
tip", and it was RIGHT: the shipped branch's tip had not moved. Both programs
answered their own question correctly. Nobody was asking THIS question:

    is there work of ours that the shipped branch cannot reach?

`daily_release.py`'s own docstring names the parent of this failure — "A merge
that stops at the fork has changed nothing anyone runs". This is that sentence
one level deeper: a merge that stops at a FEATURE BRANCH changes nothing either,
and it is harder to see, because every dashboard is green.

WHAT IT DOES NOT DO
-------------------
It does not decide whether the stranded work SHOULD ship. Merging eleven commits
that add EM signoff and dynamic IR analysis is a release decision with real blast
radius, and that belongs to a human. This program only refuses to let the
situation stay invisible.

Exit: 0 nothing stranded, 1 stranded commits found, 2 nothing could be checked.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

PROGRAM = "check_our_commits_ship"

#: Author substrings that mark a commit as OURS. An upstream commit sitting
#: unmerged on a feature branch is not a finding — that is what a fork IS.
OURS = ("reyer", "vibeic")


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def pins(bake: str) -> Dict[str, str]:
    """tool VAR -> pinned sha, read from the same file the build reads."""
    return {m.group(1): m.group(2) for m in re.finditer(
        r'variable\s+"([A-Z0-9_]+)_REF"\s*\{\s*default\s*=\s*"([a-f0-9]{7,40})"', bake)}


def _squash_carried(repo_dir: Path, pin: str, br: str) -> bool:
    """Is the branch tip's TREE already on the pin's history? (squash merge)
    Any inability to answer is False: unknown never hides a stranded commit."""
    try:
        from daily_0530 import _tree_carried
    except ImportError:
        return False
    carried_by, _evidence = _tree_carried(("git", "-C", str(repo_dir)), pin, br)
    return bool(carried_by)


def _held_for_its_pr(repo_dir: Path, br: str):
    """`(verdict, reason)` when `br` waits on a pull request, else None.

    daily_0530 no longer consolidates a branch that heads an OPEN pull request
    on the fork, or whose newest PR was closed unmerged (librelane #16/#17,
    2026-09-27): the PR is where that work is decided. Such a branch is not
    stranded -- counting it here would turn every PR under review into a red
    tick (rc=6) and push the next person to merge it by hand, which is the
    defect all over again. One predicate, imported, so the program that merges
    and the program that audits cannot disagree about which branches are owed.

    Unknown PR state returns None: the branch is reported as stranded, because
    unknown never hides a stranded commit.
    """
    try:
        from daily_0530 import (_fork_branch_pull_requests,
                                consolidation_verdict)
    except ImportError:
        return None
    verdict, why = consolidation_verdict(_fork_branch_pull_requests(
        ("git", "-C", str(repo_dir)), br))
    if verdict in ("open_pr", "declined_pr"):
        return verdict, why
    return None


def stranded(repo_dir: Path, pin: str, held: List[dict] = None) -> List[dict]:
    """Our commits reachable from some local branch but NOT from the pin.

    Commits on a branch that is waiting on its pull request are appended to
    `held` (when given) instead of being returned."""
    if not sh("git", "-C", str(repo_dir), "cat-file", "-t", pin):
        return []
    out: List[dict] = []
    seen = set()
    # every local branch, not just HEAD — the whole point is that the work is
    # somewhere other than where you are standing.
    branches = [b.strip() for b in sh(
        "git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname:short)",
        "refs/heads", "refs/remotes/origin").splitlines() if b.strip()]
    for br in branches:
        # PATCH EQUIVALENCE, not `{pin}..{br}` SHA reachability.
        #
        # This program's own docstring warns that reachability counted 273
        # stranded commits where `git cherry` counted 2 — the same fix
        # cherry-picked onto a dozen parallel branches, counted once per copy.
        # The enumeration here was doing exactly that, and it is not a rounding
        # difference. Measured on OpenROAD 2026-07-31:
        #
        #   git log {pin}..{br}        35 "stranded" commits of ours
        #   git cherry {pin} {br}      those same SHAs marked `-`
        #
        # e.g. fe6cb189b "gpl: register eco_freeze tests in Bazel BUILD" is not
        # an ancestor of the pin — and its patch IS on the shipped line as
        # bee1cf03c0, same day, same subject, cherry-picked. Reporting it as
        # stranded sends someone to merge a branch whose content already ships.
        #
        # `git cherry` prints `+` for a patch the upstream ref lacks and `-` for
        # one it already has, so only the `+` lines are candidates.
        cherry = sh("git", "-C", str(repo_dir), "cherry", pin, br)
        unique = {ln.split()[1] for ln in cherry.splitlines()
                  if ln.startswith("+") and len(ln.split()) > 1}
        if not unique:
            continue
        # TREE IDENTITY, for what patch equivalence cannot see: a SQUASH.
        #
        # A squash merge lands every byte of a branch under one commit whose
        # patch is the sum of the branch's patches, so `git cherry` marks every
        # branch commit `+`. Measured 2026-09-07 on OpenROAD, the first round
        # after 0.3.47 shipped: 7 "stranded" commits, all on
        # `postroute-drv-repair` (PR #14) and `vibeic/antenna-best-iteration`
        # (PR #15), both squash-merged into master days earlier, both
        # tree-identical to their squash commits, which the pin already
        # contained. Reporting them sends someone to merge branches whose
        # content already ships -- and the tick exited 6 every morning.
        # daily_0530 learned the same fact the same day (_tree_carried); one
        # probe, imported, so the two programs cannot disagree about it.
        if _squash_carried(repo_dir, pin, br):
            continue
        rows: List[dict] = []
        log = sh("git", "-C", str(repo_dir), "log", "--no-merges",
                 "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=short", f"{pin}..{br}")
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            sha, author, date, subject = parts
            if sha not in unique:
                continue
            if sha in seen:
                continue
            if not any(o in author.lower() for o in OURS):
                continue
            rows.append({"sha": sha[:9], "author": author, "date": date,
                         "subject": subject[:100], "branch": br, "_full": sha})
        if not rows:
            continue
        pending = _held_for_its_pr(repo_dir, br)
        for row in rows:
            full = row.pop("_full")
            if pending is not None:
                # NOT added to `seen`: the same patch on a branch without a
                # PR is still stranded there.
                if held is not None:
                    row["held"] = pending[0]
                    row["reason"] = pending[1]
                    held.append(row)
                continue
            seen.add(full)
            out.append(row)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default="/home/reyerchu/vibeic-eda")
    ap.add_argument("--forks-root", default="/home/reyerchu/vibe-ic-forks")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    bake_path = Path(args.eda_root) / "docker-bake.hcl"
    if not bake_path.is_file():
        print(f"{PROGRAM}: cannot read {bake_path}", file=sys.stderr)
        return 2
    forks_root = Path(args.forks_root)

    # VAR -> directory name. Resolved by case-insensitive match against the
    # directories that actually exist, so adding a tool needs no edit here.
    dirs = {d.name.lower(): d for d in forks_root.iterdir() if (d / ".git").is_dir()} \
        if forks_root.is_dir() else {}

    report: Dict[str, list] = {}
    held_report: Dict[str, list] = {}
    checked = 0
    for var, pin in sorted(pins(bake_path.read_text()).items()):
        cand = var.lower().replace("_", "-")
        d = dirs.get(cand) or dirs.get(var.lower()) or dirs.get(cand.replace("-", ""))
        if d is None:
            continue
        checked += 1
        held: List[dict] = []
        s = stranded(d, pin, held)
        if s:
            report[d.name] = s
        if held:
            held_report[d.name] = held

    if not checked:
        print(f"{PROGRAM}: no fork checkout matched any pin — nothing verified",
              file=sys.stderr)
        return 2

    total = sum(len(v) for v in report.values())
    summary = {"program": PROGRAM, "tools_checked": checked,
               "tools_with_stranded_work": len(report),
               "stranded_commits": total, "detail": report,
               "held_for_pull_request": held_report}
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2) + "\n")

    for tool, rows in sorted(held_report.items()):
        print(f"  {tool}: {len(rows)} commit(s) held for their pull request "
              f"(not stranded): " + ", ".join(sorted(
                  {f"{r['branch']} ({r['reason']})" for r in rows})))
    if not report:
        print(f"{PROGRAM}: {checked} tool(s) checked; every commit of ours is "
              f"reachable from the branch its pin names")
        return 0

    print(f"{PROGRAM}: {total} commit(s) of ours are NOT on the shipped branch "
          f"of {len(report)} tool(s) ({checked} checked)")
    for tool, rows in sorted(report.items()):
        print(f"\n  {tool}: {len(rows)} stranded")
        for r in rows[:15]:
            print(f"    {r['sha']}  {r['date']}  {r['subject']}")
            print(f"              on {r['branch']}, unreachable from the pin")
        if len(rows) > 15:
            print(f"    … and {len(rows) - 15} more")
    print("\n  This does NOT say the work should ship — that is a release "
          "decision with real blast radius. It says the situation must not "
          "stay invisible.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
