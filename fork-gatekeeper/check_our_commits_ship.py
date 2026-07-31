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


def stranded(repo_dir: Path, pin: str) -> List[dict]:
    """Our commits reachable from some local branch but NOT from the pin."""
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
            seen.add(sha)
            out.append({"sha": sha[:9], "author": author, "date": date,
                        "subject": subject[:100], "branch": br})
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
    checked = 0
    for var, pin in sorted(pins(bake_path.read_text()).items()):
        cand = var.lower().replace("_", "-")
        d = dirs.get(cand) or dirs.get(var.lower()) or dirs.get(cand.replace("-", ""))
        if d is None:
            continue
        checked += 1
        s = stranded(d, pin)
        if s:
            report[d.name] = s

    if not checked:
        print(f"{PROGRAM}: no fork checkout matched any pin — nothing verified",
              file=sys.stderr)
        return 2

    total = sum(len(v) for v in report.values())
    summary = {"program": PROGRAM, "tools_checked": checked,
               "tools_with_stranded_work": len(report),
               "stranded_commits": total, "detail": report}
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2) + "\n")

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
