#!/usr/bin/env python3
"""Turn capability_smoke.py's measurement into a BLOCKING release decision.

WHY THIS IS SEPARATE FROM capability_smoke.py
=============================================
`capability_smoke.py` is a MEASUREMENT: it drives each capability's real entry
point and reports WORKS / BROKEN / INCONCLUSIVE.  It has no opinion about which
breakages we have decided to live with, and it should not have one -- the moment
a probe file starts carrying exceptions, the exceptions become invisible.

This file is the POLICY, and it is deliberately the only place a breakage can be
excused.  Waivers live in `capability_waivers.txt`, by NAME, with a reason.

WHAT MAKES A GATE DIFFERENT FROM A REPORT
=========================================
`daily_release.py:SMOKE` and `build_and_regress.sh` already print things.  The
audit that produced this file (vibeic-eda#84/#87) found nine capabilities dead in
a shipped image while every existing probe passed, so "we print it" is exactly
the state being fixed.  This returns a non-zero status the caller must act on.

THREE DECISIONS, EACH ONE A FAILURE MODE SOMEONE ELSE ALREADY HIT
=================================================================
1. INCONCLUSIVE BLOCKS.  capability_smoke's own exit code counts only BROKEN, so
   a probe whose control is ALSO red exits 0.  That is "unmeasured reads as
   zero", and it is the single most common way this class of defect survives.
   Here, not knowing does not promote.

2. A STALE WAIVER IS A FAILURE.  If a waived capability now WORKS, the gate goes
   RED until the line is deleted.  A waiver list that only ever grows becomes a
   record of what we once believed; making the fix break the build is what keeps
   it a record of what is true.  (The adjacent trap -- raising a baseline COUNT
   until the gate goes green -- is why there is no count anywhere in this file
   or in the waiver file.  You waive by name or not at all.)

3. AN UNKNOWN WAIVER NAME IS A FAILURE.  A typo would otherwise waive nothing
   while reading as though it waived something, which is worse than no waiver.

Exit: 0 = every non-waived capability WORKS and every waiver is still earned.
      1 = a capability is BROKEN/INCONCLUSIVE unwaived, or a waiver is stale or
          unknown.
      2 = capability_smoke could not run the image at all (RC_NOIMAGE), i.e. we
          measured nothing -- which is a failure, not a pass.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SMOKE = os.path.join(HERE, "capability_smoke.py")
WAIVERS = os.path.join(HERE, "capability_waivers.txt")
VERSION_FILE = os.path.join(HERE, os.pardir, "VERSION")

RC_OK, RC_FAIL, RC_NOIMAGE = 0, 1, 2


def _example_image() -> str:
    """An example tag for --help, READ from VERSION rather than written here.

    A literal `ghcr.io/vibeic/vibeic-eda:0.2.NN` in this file is exactly the
    defect this gate exists to catch, one level up: it is a pointer at a
    specific image that nothing keeps in step with the one being shipped, so it
    goes stale silently and then describes the wrong artefact. The repo's own
    `test_image_version_history_line.py` fails on an unregistered live pointer,
    and it was right to -- the first version of this file hardcoded a tag that
    did not even exist yet.
    """
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return "ghcr.io/vibeic/vibeic-eda:" + fh.read().strip()
    except OSError:
        return "ghcr.io/vibeic/vibeic-eda:<version>"


def read_waivers(path: str) -> dict[str, str]:
    """name -> reason.  Comments and blanks ignored; `name  # reason` supported."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, reason = line.partition("#")
            out[name.strip()] = reason.strip() or "(no reason given)"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="image to probe, e.g. " + _example_image())
    ap.add_argument("--waivers", default=WAIVERS)
    ap.add_argument("--json", metavar="FILE", help="keep the raw probe table here")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()

    waived = read_waivers(a.waivers)
    tmp = a.json or os.path.join(tempfile.mkdtemp(prefix="capgate-"), "r.json")

    proc = subprocess.run(
        [sys.executable, SMOKE, a.image, "--json", tmp],
        capture_output=True, text=True, timeout=a.timeout)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)

    if proc.returncode == RC_NOIMAGE or not os.path.exists(tmp):
        print(f"\ncapability-gate: FAIL -- {a.image} could not be probed at all; "
              f"nothing was measured, so nothing is proven", file=sys.stderr)
        return RC_NOIMAGE

    with open(tmp, encoding="utf-8") as fh:
        rows = json.load(fh)
    seen = {r["capability"]: r["verdict"] for r in rows}

    unwaived_bad, stale, unknown = [], [], []
    for r in rows:
        cap, verdict = r["capability"], r["verdict"]
        if verdict == "WORKS":
            if cap in waived:
                stale.append(cap)
        elif cap not in waived:
            # BROKEN and INCONCLUSIVE both land here on purpose -- see decision 1.
            unwaived_bad.append(f"{cap} [{verdict}] {r.get('reason') or ''}"[:160])
    unknown = [w for w in waived if w not in seen]

    print(f"\ncapability-gate on {a.image}")
    print(f"  probes            : {len(rows)}")
    print(f"  WORKS             : {sum(1 for v in seen.values() if v == 'WORKS')}")
    print(f"  waived (still red): {sum(1 for c, v in seen.items() if c in waived and v != 'WORKS')}")
    for cap in sorted(c for c, v in seen.items() if c in waived and v != "WORKS"):
        print(f"      - {cap}  # {waived[cap]}")

    ok = True
    if unwaived_bad:
        ok = False
        print("\n  BLOCKING -- not working and not waived:")
        for line in unwaived_bad:
            print(f"      {line}")
    if stale:
        ok = False
        print("\n  BLOCKING -- STALE WAIVER (these WORK now; delete the line):")
        for cap in stale:
            print(f"      {cap}")
    if unknown:
        ok = False
        print("\n  BLOCKING -- waiver names no probe knows (typo?):")
        for cap in unknown:
            print(f"      {cap}")

    print(f"\ncapability-gate: {'PASS' if ok else 'FAIL'}")
    return RC_OK if ok else RC_FAIL


if __name__ == "__main__":
    sys.exit(main())
