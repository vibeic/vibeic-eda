#!/usr/bin/env python3
"""fork_gap_report — the two questions that must be answerable every day.

    Q1  how far behind upstream is the SHIPPED IMAGE?
    Q2  do our own commits and bug fixes actually REACH that image?

Both must be able to read ZERO, and a zero must mean the thing it says.

WHY THIS IS A PROGRAM
=====================
These were measured by hand and the hand got them wrong twice in one evening:

1. A missing pin was read as a zero gap. The first cut took the pin from the
   ledger's `ref` field and, when that was absent, fell back to the clone's own
   HEAD — which makes `pin..HEAD` identically 0. All six lagging tools then
   reported "purely a sync problem" and the 31-commit RELEASE lag vanished. An
   absent measurement rendered as the reassuring answer, which is the defect this
   whole campaign exists to remove.

2. "Behind" was read as one number when it is two. `behind_commits` measures
   PIN -> UPSTREAM, and that distance has two independent causes that need
   opposite fixes:

       SYNC LAG      our fork is behind upstream        -> merge upstream in
       RELEASE LAG   the image's pin is behind OUR fork -> bump the pin, rebuild

   Measured 2026-08-02: of 47 commits behind, 20 were sync and 31 were release —
   including yosys, whose fork was perfectly in sync while the image still
   lacked 3 of our own commits. "Sync harder" would not have moved that number.

RULES THIS ENCODES
==================
- The pin comes from the Dockerfile's own `ARG <TOOL>_REF`, because that is what
  the image is BUILT FROM. Any other source describes something else.
- A pin that cannot be found is `null`, never 0, and makes the run exit 2.
- A clone that cannot be read is `null`, never 0.
- `integrated=false` (the image does not build from our fork at all) is reported
  as its own state, because a fork that ships nothing has no meaningful pin gap
  and must not be silently counted as "0 behind".

EXIT
    0  both headline numbers are zero
    1  a gap exists (the report says which kind, per tool)
    2  something could not be measured — NOT the same as zero
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

ARG_RE = re.compile(r"ARG\s+([A-Z0-9_]*?)_REF\s*=\s*([0-9a-f]{7,40})")


def _git(repo: Path, *args: str, timeout: int = 60) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def pins_from_dockerfiles(repo: Path, ref: str = "origin/main") -> Dict[str, str]:
    """Every `ARG <X>_REF=<sha>` the image build declares, keyed by the ARG stem.

    Read from the committed tree, not the working copy: the question is what the
    image ships, and an uncommitted edit ships nothing.
    """
    out: Dict[str, str] = {}
    listing = _git(repo, "ls-tree", "-r", "--name-only", ref) or ""
    for path in listing.splitlines():
        if not path.endswith("Dockerfile") and not path.endswith(".hcl"):
            continue
        body = _git(repo, "show", f"{ref}:{path}")
        if not body:
            continue
        for stem, sha in ARG_RE.findall(body):
            out.setdefault(stem, sha)
    return out


def count(repo: Path, a: str, b: str) -> Optional[int]:
    """commits in b that are not in a. None when it cannot be answered."""
    v = _git(repo, "rev-list", "--count", f"{a}..{b}")
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def upstream_head(clone: Path) -> Optional[str]:
    for cand in ("upstream/master", "upstream/main"):
        if _git(clone, "rev-parse", "--verify", "-q", cand):
            return cand
    br = _git(clone, "symbolic-ref", "--short", "HEAD")
    if br and _git(clone, "rev-parse", "--verify", "-q", f"upstream/{br}"):
        return f"upstream/{br}"
    return None


def ours_past_the_pin(clone: Path, pin: str, up: str) -> Optional[List[dict]]:
    """Our commits that the image does NOT ship: `pin..HEAD` minus what upstream has.

    Q2 was first answered with the ledger's `integrated` flag — "does the image
    build from our fork at all". It does, for OpenROAD and iverilog and three
    more, and the answer published was "0 stranded" while a fix of ours from that
    same morning sat past the pin, unbuilt. `integrated` is a fact about the
    Dockerfile; where the PIN STOPPED is a different fact, and it is the one the
    question asks about.

    DERIVED, not author-matched. Our commits are by definition the ones upstream
    does not carry, so a set difference finds them — including a commit an
    outside contributor landed on our fork, which an @vibeic/@defintek email
    pattern silently drops.

    `merge` is recorded per commit rather than filtered out here, because the two
    populations need opposite handling: a merge of ours whose CONTENT is
    upstream's is not our fix going unshipped, and counting it would raise an
    alarm after every routine 05:30 sync. The headline counts substantive only;
    the merge count stays visible so the lag is never invisible either.

    None (never []) when it cannot be derived — an unresolvable pin is NOT zero.
    """
    out = _git(clone, "rev-list", f"{pin}..HEAD", "--not", up, timeout=120)
    if out is None:
        return None
    rows: List[dict] = []
    for sha in out.split():
        meta = _git(clone, "show", "-s", "--format=%p\x1f%an\x1f%ad\x1f%s",
                    "--date=short", sha, timeout=30)
        if meta is None:
            return None                      # partial truth is not truth here
        parents, an, ad, subj = (meta.split("\x1f") + ["", "", "", ""])[:4]
        rows.append({"sha": sha[:12], "merge": len(parents.split()) >= 2,
                     "author": an, "date": ad, "subject": subj})
    return rows


def analyse(repo: Path, forks_root: Path, ledger: Path, fetch: bool) -> dict:
    pins = pins_from_dockerfiles(repo)
    rows: List[dict] = []
    for lf in sorted(ledger.glob("*.json")):
        if lf.name == "index.json":
            continue
        try:
            led = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tool = led.get("tool") or lf.stem
        clone = forks_root / tool
        row = {"tool": tool, "integrated": bool(led.get("integrated")),
               "pin_source": "ARG",
               "ahead": led.get("ahead"), "pin": None,
               "sync_lag": None, "release_lag": None, "image_behind": None,
               "note": None}

        # the pin, by the ARG stem that matches this tool (case/dash-insensitive)
        key = tool.upper().replace("-", "_")
        row["pin"] = pins.get(key) or pins.get(key.replace("_", "")) or None
        if row["pin"] is None:
            for k, v in pins.items():
                if k.replace("_", "") == key.replace("_", ""):
                    row["pin"] = v
                    break
        # A fork can be pinned WITHOUT a hex ARG of its own, two legitimate ways.
        # Neither is "unpinned", and neither may fall through to a HEAD fallback —
        # that fallback is what made every gap read 0 the first time this was
        # measured by hand.
        #   * VENDORED: it arrives inside another fork's pinned ref as a submodule
        #     (OpenSTA lives at src/sta inside OpenROAD, pinned via OPENROAD_REF).
        #   * PINNED TO A BRANCH: `ARG X_REF=main` — a name, not a sha, so the hex
        #     pattern above cannot see it (the three ASAP7 trees).
        # The ledger derives both; take its answer only when it resolves in the
        # clone, so a stale or bogus value still lands on NOT MEASURED.
        if row["pin"] is None:
            cand = led.get("vendored_host_ref") or led.get("pinned_ref_full") or led.get("pinned_ref")
            if cand and clone.is_dir() and _git(clone, "rev-parse", "--verify", "-q", f"{cand}^{{commit}}"):
                row["pin"] = cand
                row["pin_source"] = ("vendored in " + str(led.get("vendored_in"))
                                     if led.get("vendored_in") else "branch pin")

        if not clone.is_dir():
            row["note"] = "no clone — NOT MEASURED"
            rows.append(row); continue
        if fetch:
            _git(clone, "fetch", "-q", "--all", timeout=180)
        up = upstream_head(clone)
        if up is None:
            row["note"] = "no upstream remote — NOT MEASURED"
            rows.append(row); continue

        row["sync_lag"] = count(clone, "HEAD", up)
        if row["pin"]:
            row["release_lag"] = count(clone, row["pin"], "HEAD")
            row["image_behind"] = count(clone, row["pin"], up)
            ours = ours_past_the_pin(clone, row["pin"], up)
            row["ours_unshipped"] = None if ours is None else len(ours)
            row["ours_unshipped_substantive"] = (
                None if ours is None else len([c for c in ours if not c["merge"]]))
            row["unshipped_commits"] = (
                [] if ours is None else [c for c in ours if not c["merge"]])
        elif not row["integrated"]:
            row["note"] = "image does not build from our fork (no ARG pin) — see vibeic-eda#60"
        else:
            row["note"] = "PIN NOT FOUND in any Dockerfile — NOT MEASURED, not zero"
        rows.append(row)

    for r in rows:
        r.setdefault("ours_unshipped", None)
        r.setdefault("ours_unshipped_substantive", None)
        r.setdefault("unshipped_commits", [])
    measurable = [r for r in rows if r["image_behind"] is not None]
    unmeasured = [r for r in rows
                  if r["image_behind"] is None and r["integrated"]]
    not_built = [r for r in rows if not r["integrated"]]
    stranded = [r for r in not_built if (r.get("ahead") or 0) > 0]

    return {
        "q1_image_behind_upstream": sum(r["image_behind"] for r in measurable),
        "q1_forks_behind": len([r for r in measurable if r["image_behind"]]),
        "q1_sync_lag": sum(r["sync_lag"] or 0 for r in measurable),
        "q1_release_lag": sum(r["release_lag"] or 0 for r in measurable),
        "q1_unmeasured": [r["tool"] for r in unmeasured],
        "q2_forks_not_built_from_ours": [r["tool"] for r in not_built],
        "q2_our_commits_stranded": sum(r.get("ahead") or 0 for r in stranded),
        "q2_ours_past_the_pin": sum(r["ours_unshipped"] or 0 for r in rows),
        "q2_ours_past_the_pin_substantive":
            sum(r["ours_unshipped_substantive"] or 0 for r in rows),
        "q2_unshipped_commits": [dict(c, tool=r["tool"])
                                 for r in rows for c in r["unshipped_commits"]],
        "q2_unmeasured_ship": [r["tool"] for r in rows
                               if r["integrated"] and r["pin"]
                               and r["ours_unshipped"] is None],
        "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=Path("/home/reyerchu/vibeic-eda"))
    ap.add_argument("--forks-root", type=Path, default=Path("/home/reyerchu/vibe-ic-forks"))
    ap.add_argument("--ledger", type=Path,
                    default=Path.home() / ".cache/eda-fork-gatekeeper/ledger")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)

    if not a.ledger.is_dir():
        print("fork_gap_report: rc=2 NOT MEASURED — no ledger"); return 2
    rep = analyse(a.repo, a.forks_root, a.ledger, not a.no_fetch)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")

    print(f"{'TOOL':<24}{'image behind':>13}{'= sync':>8}{'+ release':>11}   state")
    for r in sorted(rep["rows"], key=lambda x: -(x["image_behind"] or 0)):
        if r["note"]:
            print(f"{r['tool']:<24}{'—':>13}{'—':>8}{'—':>11}   {r['note']}")
            continue
        if not r["image_behind"]:
            continue
        print(f"{r['tool']:<24}{r['image_behind']:>13}{r['sync_lag']:>8}"
              f"{r['release_lag']:>11}   "
              f"{'sync' if not r['release_lag'] else ('release' if not r['sync_lag'] else 'both')}")

    print()
    print(f"  Q1  image behind upstream : {rep['q1_image_behind_upstream']} "
          f"across {rep['q1_forks_behind']} fork(s)"
          f"   [sync {rep['q1_sync_lag']} · release {rep['q1_release_lag']}]")
    print(f"  Q2  forks the image does NOT build from : "
          f"{len(rep['q2_forks_not_built_from_ours'])}"
          f" ({', '.join(rep['q2_forks_not_built_from_ours']) or 'none'})")
    print(f"      our commits stranded in them        : {rep['q2_our_commits_stranded']}")
    print(f"      our commits PAST THE PIN (not shipped): "
          f"{rep['q2_ours_past_the_pin_substantive']} substantive"
          f"  (+{rep['q2_ours_past_the_pin'] - rep['q2_ours_past_the_pin_substantive']}"
          f" merge commits, content is upstream's)")
    for c in rep["q2_unshipped_commits"]:
        print(f"          {c['tool']}/{c['sha']}  {c['date']}  {c['author']}")
        print(f"              {c['subject']}")
    unmeasured = rep["q1_unmeasured"] + rep["q2_unmeasured_ship"]
    if unmeasured:
        print(f"  NOT MEASURED (never counted as zero) : {', '.join(sorted(set(unmeasured)))}")
        return 2
    return 0 if (rep["q1_image_behind_upstream"] == 0
                 and not rep["q2_forks_not_built_from_ours"]
                 and rep["q2_ours_past_the_pin_substantive"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
