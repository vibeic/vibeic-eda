#!/usr/bin/env python3
"""What has upstream FIXED that our pinned fork still ships?

WHY THIS EXISTS (vibe-ic#553)
=============================
The gatekeeper's daily tick asks "should we adopt this upstream commit?" — a
selective-adoption question, answered per commit by `assess_release.py`. It has
never asked the other direction: **is upstream carrying a fix for a bug we still
ship?**

vibe-ic#551 is what surfaced the gap. Chasing an `rsz::stitchTrees` segfault to
file it upstream, we found upstream had closed it on 2026-07-13 (`5b9e0a371`,
three lines) and our pin predates that by 772 commits. We had not hit it only
because a *different* crash in our build wins the race on the same code path.
That nearly cost a day minimising a reproducer for a bug fixed in July.

WHAT THIS IS NOT
================
Not auto-adoption, and not a rebase. Being behind is deliberate — a fork that
chases master is unbuildable, and rebasing 772 commits under 51 of our own is a
large risky job whose payoff is unmeasured. This produces a LIST, so a crash we
hit can be checked against upstream's history before anyone spends a day on it.

HONEST SCOPE, because the output invites over-reading
=====================================================
* **The subject-line match is a proxy, not a classification.** It calls a
  test-only change a fix and misses one titled "handle empty input". The number
  is an order of magnitude.
* **GitHub's compare API caps a response at 250 commits.** A fork 772 behind
  reports 250 and says so. Reporting a sample as a population is the exact
  failure that produced a wrong fork count in vibeic-eda#15; every count here
  carries `truncated` when it hits the cap.
* **`paths` needs the per-commit endpoint** and is fetched only for ranked
  candidates, because 250 extra API calls per tool per tick is not free.

Exit: 0 survey completed, 1 a fork could not be surveyed, 2 nothing surveyed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

RC_OK, RC_PARTIAL, RC_NOTHING = 0, 1, 2

#: GitHub returns at most this many commits from the compare endpoint. Named
#: rather than inline so the truncation disclosure below cannot drift from it.
COMPARE_CAP = 250

#: Subject-line signals for "this is a defect fix". Deliberately broad — a false
#: positive costs a reader one line, a false negative costs what #551 cost.
_FIX_RE = re.compile(
    r"\b(fix(e[sd])?|crash|segfault|sigsegv|sigill|abort|assert|null|"
    r"leak|regression|bug|hang|deadlock|overflow|uninitiali[sz]ed)\b", re.I)

#: Subtrees whose defects can reach our flow. A fix in `src/gui` matters less to
#: a headless sign-off run than one in the router or the resizer. This is a
#: RANKING input, never a filter: an unranked commit is still listed.
RELEVANT_PATHS: Dict[str, tuple] = {
    "OpenROAD": ("src/drt/", "src/rsz/", "src/grt/", "src/dpl/", "src/ant/",
                 "src/psm/", "src/cts/", "src/sta/", "src/odb/"),
    "yosys": ("frontends/", "passes/", "backends/", "kernel/"),
    "klayout": ("src/db/", "src/lay/", "src/plugins/"),
    "iverilog": ("", ),          # small enough that everything is relevant
    "verilator": ("src/", ),
    "ngspice": ("src/", ),
    "magic": ("", ),
    "netgen": ("", ),
}


def _sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def _gh_json(path: str) -> Optional[dict]:
    rc, out = _sh(["gh", "api", path])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def pinned_refs(eda_root: Path) -> Dict[str, str]:
    """fork repo name -> 40-char SHA, read from the Dockerfiles that build it.

    Pairs each `ARG <TOOL>_REF=<sha>` with the `github.com/vibeic/<repo>` clone
    that consumes it, by NAME rather than by position: `tools/lvs/Dockerfile`
    builds two forks (magic + netgen) from one file, so a first-match parse
    silently drops the second.

    Two things my first version got wrong, both of which made the survey report
    a smaller gap than exists — the failure this program is written to prevent,
    one level up:
      * an end-of-line anchor after the SHA: every pin here carries a trailing
        `# pinned; ...` comment,
        so four of six tools matched nothing;
      * one ref per file: `lvs` has two.
    """
    pins: Dict[str, str] = {}
    for df in sorted((eda_root / "tools").glob("*/Dockerfile")):
        text = df.read_text(errors="replace")
        refs = dict(re.findall(
            r"^ARG\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})", text, re.M))
        repos = re.findall(
            r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text)
        for repo in dict.fromkeys(repos):          # order-preserving unique
            key = repo.upper().replace("-", "_")
            sha = refs.get(key)
            if sha is None:                        # e.g. repo `OpenROAD` vs ARG `OPENROAD`
                for k, v in refs.items():
                    if k.replace("_", "") == key.replace("_", ""):
                        sha = v
                        break
            if sha:
                pins[repo] = sha
    return pins


def survey_one(repo: str, ref: str) -> dict:
    """Upstream commits reachable from their default branch but not from `ref`."""
    meta = _gh_json(f"repos/vibeic/{repo}")
    if not meta or not meta.get("parent"):
        return {"repo": repo, "error": "no upstream parent recorded"}
    parent = meta["parent"]["full_name"]
    branch = meta["parent"].get("default_branch", "master")
    owner = parent.split("/")[0]

    cmp_doc = _gh_json(f"repos/vibeic/{repo}/compare/{ref}...{owner}:{branch}")
    if cmp_doc is None:
        return {"repo": repo, "upstream": parent, "error": "compare failed"}

    behind = cmp_doc.get("total_commits", 0)
    commits = cmp_doc.get("commits", []) or []
    truncated = len(commits) >= COMPARE_CAP or behind > len(commits)

    rel = RELEVANT_PATHS.get(repo, ("", ))
    fixes: List[dict] = []
    for c in commits:
        subject = (c.get("commit", {}).get("message") or "").split("\n")[0]
        if not _FIX_RE.search(subject):
            continue
        fixes.append({"sha": c.get("sha", "")[:9], "subject": subject[:120],
                      "date": (c.get("commit", {}).get("author", {})
                               .get("date", ""))[:10]})
    return {
        "repo": repo, "upstream": parent, "branch": branch, "pin": ref[:9],
        "behind": behind,
        "sampled": len(commits),
        # Named, not implied: a caller that reads `fix_candidates` without this
        # is reading a sample as a population.
        "truncated": truncated,
        "truncation_note": (
            f"upstream is {behind} ahead but GitHub's compare endpoint returned "
            f"{len(commits)}; counts below are over that sample only"
            if truncated else ""),
        "fix_candidates": len(fixes),
        "relevant_subtrees": list(rel),
        "commits": fixes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None)
    ap.add_argument("--top", type=int, default=8,
                    help="commits to print per fork (all go to --json)")
    a = ap.parse_args(argv)

    pins = pinned_refs(Path(a.eda_root))
    if not pins:
        print("[NOT CHECKED] no pinned refs found under tools/*/Dockerfile — "
              "nothing was surveyed, which is not a clean result",
              file=sys.stderr)
        return RC_NOTHING

    results = [survey_one(repo, ref) for repo, ref in sorted(pins.items())]
    errored = [r for r in results if r.get("error")]

    total_behind = sum(r.get("behind", 0) for r in results)
    total_fixes = sum(r.get("fix_candidates", 0) for r in results)
    any_trunc = any(r.get("truncated") for r in results)

    print(f"inbound_survey: {len(results)} fork(s), {total_behind} upstream "
          f"commit(s) our pins lack, {total_fixes} whose subject reads as a "
          f"defect fix")
    if any_trunc:
        print("  NOTE: at least one fork exceeded GitHub's 250-commit compare "
              "cap; its fix count is over a SAMPLE, not the whole gap")
    for r in results:
        if r.get("error"):
            print(f"  {r['repo']}: ERROR {r['error']}", file=sys.stderr)
            continue
        flag = "  [SAMPLED]" if r["truncated"] else ""
        print(f"  {r['repo']:<12} pin {r['pin']}  behind {r['behind']:>4}  "
              f"fix-like {r['fix_candidates']:>3} of {r['sampled']}{flag}")
        for c in r["commits"][:a.top]:
            print(f"      {c['sha']}  {c['date']}  {c['subject'][:88]}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "inbound_survey", "forks": results,
             "total_behind": total_behind, "total_fix_candidates": total_fixes,
             "any_truncated": any_trunc}, indent=2) + "\n", encoding="utf-8")

    if errored:
        print(f"[PARTIAL] {len(errored)} fork(s) could not be surveyed",
              file=sys.stderr)
        return RC_PARTIAL
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
