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


def ours_past_the_pin(clone: Path, pin: str, up: str,
                      tip: str) -> Optional[List[dict]]:
    """Our commits that the image does NOT ship: `pin..<published tip>` minus
    what upstream has.

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
    out = _git(clone, "rev-list", f"{pin}..{tip}", "--not", up, timeout=120)
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


def vendored_pin(forks_root: Path, led: dict) -> Optional[str]:
    """The effective pin of a fork that reaches the image INSIDE another fork.

    OpenSTA has no `ARG OPENSTA_REF`, because the image never clones it: OpenROAD
    carries it at `src/sta` and the build compiles `//src/sta:opensta` out of that
    tree. Its pin is therefore whatever commit OpenROAD's submodule points at, at
    OpenROAD's own pin — a real, exact answer that this program reported as NOT
    MEASURED simply because it looked in one place.

    Resolved here rather than read from the ledger's `pinned_ref_full`, so this
    program does not inherit another program's answer. The ledger's value is then
    compared against it, and a disagreement is surfaced rather than silently
    broken in favour of one side: two derivations of one fact that disagree is a
    finding.

    None when it cannot be resolved. Never a fallback to the host ref or to HEAD —
    the wrong-but-plausible pin is exactly what made every lagging tool read
    "0 behind" the first time this was measured by hand.
    """
    host, path = led.get("vendored_in"), led.get("vendored_path")
    host_ref = led.get("vendored_host_ref")
    if not (host and path and host_ref):
        return None
    row = _git(forks_root / host, "ls-tree", host_ref, path, timeout=60)
    if not row:
        return None
    parts = row.split()
    # `160000 commit <sha>\t<path>` — a gitlink. Anything else is not a submodule
    # pointer and must not be read as one.
    if len(parts) < 3 or parts[0] != "160000" or parts[1] != "commit":
        return None
    return parts[2]


def published_tip(clone: Path, led: dict) -> Optional[str]:
    """Our fork's PUBLISHED line — never the clone's HEAD.

    These clones are shared. `HEAD` is whatever the last process to touch the
    directory left checked out, and that is not a fact about our fork.

    Measured 2026-08-02, both directions in one sweep, on the run that produced
    this function:

      OpenSTA   HEAD sat on `fix/max-fanout-applicability-…`, a branch another
                session had created and committed to 25 minutes earlier. Counting
                `pin..HEAD` reported that in-progress commit as "our fix is not in
                the shipped image" — work that was never claimed to be shipped and
                may never land.

      OpenROAD  HEAD sat one commit BEHIND `origin/master`, because a fix had just
                merged and this clone had not fetched. The same count would have
                MISSED a landed fix that genuinely is not in the image.

    An overcount and an undercount from one wrong reference. `origin/<branch>` is
    the tip we actually publish, so it is the only defensible answer to "have our
    commits reached the image".

    None when it cannot be resolved — never a fall back to HEAD.
    """
    cands = []
    if led.get("vibeic_branch"):
        cands.append("origin/%s" % led["vibeic_branch"])
    cands += ["origin/master", "origin/main"]
    for c in cands:
        if _git(clone, "rev-parse", "--verify", "-q", c):
            return c
    return None


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
               "pin_source": "ARG", "pin_disagreement": None,
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

        # A vendored fork has no ARG of its own; its pin lives one level in.
        if row["pin"] is None:
            vp = vendored_pin(forks_root, led)
            if vp:
                row["pin"] = vp
                row["pin_source"] = (
                    f"{led.get('vendored_in')}@"
                    f"{(led.get('vendored_host_ref') or '')[:12]}:{led.get('vendored_path')}")
                claimed = led.get("pinned_ref_full")
                if claimed and claimed != vp:
                    row["pin_disagreement"] = (
                        f"ledger says {claimed[:12]}, the submodule pointer says {vp[:12]}")

        if not clone.is_dir():
            row["note"] = "no clone — NOT MEASURED"
            rows.append(row); continue
        if fetch:
            _git(clone, "fetch", "-q", "--all", timeout=180)
        up = upstream_head(clone)
        if up is None:
            row["note"] = "no upstream remote — NOT MEASURED"
            rows.append(row); continue

        tip = published_tip(clone, led)
        if tip is None:
            row["note"] = "no published origin branch — NOT MEASURED"
            rows.append(row); continue
        row["tip"] = tip
        row["sync_lag"] = count(clone, tip, up)
        if row["pin"]:
            row["release_lag"] = count(clone, row["pin"], tip)
            row["image_behind"] = count(clone, row["pin"], up)
            ours = ours_past_the_pin(clone, row["pin"], up, tip)
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


# ── vibeic-eda#60 — the unpinned four, and the contradiction they make cheap ──
#
# `open_pdks`, `ciel`, `sv2v` and `IHP-Open-PDK` are forks the image does not
# build from: no `ARG <TOOL>_REF`, no clone, the base image's copies. All four
# carry ZERO patches today, so nothing is lost — and that is why it is easy to
# miss. Whether to wire them in or stop forking them is the owner's call
# (vibeic-eda#60 states both options and declines to pick); until it is made,
# this is a standing state and not a new finding.
#
# Recorded as a baseline that MAY ONLY SHRINK, the same shape this org uses for
# `flow_step_can_fail_check` and `checker_execution_wiring_audit`. Without it
# this report is permanently rc=1 for a reason nobody is acting on, and a report
# that is always red is one people route around — which would hide the condition
# below on the day it first becomes true.
#
# EMPTY AS OF 2026-08-04 — the debt is paid, not waived. All four now carry a
# real pin, verified in the Dockerfiles rather than taken from this report's own
# "baseline shrank" note:
#
#     ARG OPEN_PDKS_REF=b344c97e...      ARG CIEL_REF=714d1bbb...
#     ARG SV2V_REF=6662fa5d...           ARG IHP_OPEN_PDK_REF=22f2a25f...
#
# The report had been telling us this and exiting 1 for it, which is the design
# working; what it could not do was update itself. Left non-empty, the shrink
# note fires on every run forever and the report is permanently red — the exact
# condition this comment warns about two paragraphs up.
#
# The MECHANISM is not deleted along with the contents. Its tests now build a
# synthetic baseline instead of reading this set, because tests that draw their
# fixture from the live register stop testing anything the moment the register
# empties — the debt being paid would silently remove the guard that catches the
# next unpinned fork.
_UNPINNED_BASELINE: frozenset = frozenset()

# ── the contradiction, which NO baseline excuses ─────────────────────────────
#
# A fork with no pin that is nonetheless AHEAD is carrying a patch that cannot
# ship. The ledger will report `ahead=1` truthfully and the row will read like
# success. Today the condition is unreachable for all four — zero divergence
# from upstream — which is exactly why guarding it now is cheap, and why it is
# separate from the baseline above: being on a known list excuses NOT BEING
# BUILT FROM. It does not excuse carrying a patch that cannot reach a user.


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
    disagreed = [r for r in rep["rows"] if r.get("pin_disagreement")]
    for r in disagreed:
        print(f"  PIN DISAGREEMENT {r['tool']}: {r['pin_disagreement']}")

    unmeasured = (rep["q1_unmeasured"] + rep["q2_unmeasured_ship"]
                  + [r["tool"] for r in disagreed])
    if unmeasured:
        print(f"  NOT MEASURED (never counted as zero) : {', '.join(sorted(set(unmeasured)))}")
        return 2
    # #60 — a fork carrying a patch it cannot ship. No baseline excuses this:
    # the baseline covers "not built from", not "patched and unshippable".
    _stranded_rows = [r for r in rep["rows"]
                      if not r["integrated"] and (r.get("ahead") or 0) > 0]
    if _stranded_rows:
        print()
        print(f"  [FAIL] {len(_stranded_rows)} fork(s) carry commits that CANNOT "
              f"SHIP — no ARG pin, so the image does not build from them:")
        for r in _stranded_rows:
            print(f"      {r['tool']}: ahead={r['ahead']} with no pin. Either "
                  f"wire it into the Dockerfile or drop the patch; a fork that "
                  f"is patched and unbuilt reports success while shipping "
                  f"nothing (vibeic-eda#60).")
        return 1

    # The four unpinned forks are a recorded, owner-pending state. NEW ones are
    # not, and a baseline that grew is a regression accommodated rather than
    # fixed — so both directions are checked.
    _unpinned = set(rep["q2_forks_not_built_from_ours"])
    _new = sorted(_unpinned - _UNPINNED_BASELINE)
    _gone = sorted(_UNPINNED_BASELINE - _unpinned)
    if _gone:
        print(f"  [NOTE] baseline shrank — now pinned or no longer forked: "
              f"{', '.join(_gone)}. Remove them from _UNPINNED_BASELINE.")
    if _new:
        print(f"  [FAIL] {len(_new)} fork(s) newly not built from ours: "
              f"{', '.join(_new)}")
        return 1

    return 0 if (rep["q1_image_behind_upstream"] == 0
                 and rep["q2_ours_past_the_pin_substantive"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
