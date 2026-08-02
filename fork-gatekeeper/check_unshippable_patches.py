#!/usr/bin/env python3
"""check_unshippable_patches.py — a patch we carry that cannot reach the image.

vibeic-eda#60. We fork `ciel`, `open_pdks`, `sv2v` and `IHP-Open-PDK`, and the
image ships all four — from the BASE image, not from our forks. Nothing is lost
today because all four are byte-identical to upstream:

    ciel          714d1bbb626d == fossi-foundation/ciel@main
    open_pdks     c0eb16d5d3d7 == fossi-foundation/open-pdks@main
    sv2v          6662fa5da71f == zachjs/sv2v@master
    IHP-Open-PDK  22f2a25f1734 == IHP-GmbH/IHP-Open-PDK@main

THE DEFECT IS LATENT, AND THAT IS WHY IT NEEDS A GUARD NOW. It activates on the
first patch: at that moment the ledger reports `ahead=1` — our patch, correctly
counted, on a fork the image does not build from — and the row reads as success.
A number that goes UP is the last place anyone looks for a failure.

    ahead > 0  AND  integrated = false     is a contradiction

`integrated` means REACHES THE SHIPPED IMAGE, by either route: an ARG pin of its
own, or vendored inside one. `ahead` counts commits of ours that upstream lacks.
Both true at once says we are maintaining work that cannot ship.

WHAT THIS DOES NOT DECIDE. Whether each fork should be wired in (pin it, build
from it) or dropped (we do not intend to patch a PDK data repo, so carrying a
fork of it is not honest either) is an owner call, and #60 says so. This guard
only refuses the third state — a fork that exists, is tracked, is reported on a
public page, and cannot reach the image — once it starts costing something.

MEASURED at the time of writing: 0 forks violate it, and exactly four are
`integrated=false`. The guard is added while it is free, which is the only time
adding one is cheap.

Exit codes: 0 PASS, 1 at least one unshippable patch, 2 the question could not
be put (no ledger to read).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gk_state                                            # noqa: E402

#: IMPORTED, not re-derived. The first version of this file spelled the state
#: path itself and got it wrong (`~/.gatekeeper` against the real
#: `~/.cache/eda-fork-gatekeeper`), so the guard reported "the question could
#: not be put" on a machine where the ledger was sitting right there. Two
#: derivations of "where is the state" drift; one does not.
DEFAULT_LEDGER = gk_state.state_dir() / "ledger"


def load_ledgers(ledger_dir: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in sorted(ledger_dir.glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def violations(ledgers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Forks carrying commits of ours that cannot reach the image.

    `ahead` is read strictly: None means it was never measured, and an
    unmeasured count is NOT a violation — reporting one would turn "we could not
    tell" into "we found something", which is the failure mode this repo keeps
    fixing in the other direction.
    """
    bad = []
    for d in ledgers:
        ahead = d.get("ahead")
        if not isinstance(ahead, int) or ahead <= 0:
            continue
        if d.get("integrated"):
            continue
        bad.append({"tool": d.get("tool") or d.get("repo") or "?",
                    "ahead": ahead,
                    "branch": d.get("vibeic_branch"),
                    "role": (d.get("role") or "")[:120]})
    return bad


def unmeasured(ledgers: List[Dict[str, Any]]) -> List[str]:
    """Forks whose `ahead` was never measured, so the invariant could not be put
    to them. Disclosed rather than counted as clean."""
    return sorted(str(d.get("tool") or "?") for d in ledgers
                  if not isinstance(d.get("ahead"), int)
                  and not d.get("integrated"))


def report(ledger_dir: Path) -> Tuple[int, str]:
    if not ledger_dir.is_dir():
        return 2, (f"check_unshippable_patches: no ledger at {ledger_dir} — the "
                   f"question could not be put. Run discover_forks first.")
    ledgers = load_ledgers(ledger_dir)
    if not ledgers:
        return 2, (f"check_unshippable_patches: {ledger_dir} holds no readable "
                   f"ledger — the question could not be put.")
    bad = violations(ledgers)
    unk = unmeasured(ledgers)
    tail = (f"\n  ({len(unk)} unpinned fork(s) have no measured `ahead`, so the "
            f"invariant could not be put to them: {', '.join(unk)})" if unk else "")
    if not bad:
        return 0, (f"check_unshippable_patches: PASS — {len(ledgers)} fork(s), "
                   f"none carries commits it cannot ship{tail}")
    lines = [f"check_unshippable_patches: {len(bad)} fork(s) carry patches that "
             f"CANNOT REACH THE IMAGE"]
    for b in bad:
        lines.append(f"  {b['tool']}: ahead={b['ahead']} on "
                     f"{b['branch'] or '(no branch recorded)'}, but the image "
                     f"does not build from this fork (integrated=false)")
        if b["role"]:
            lines.append(f"      role: {b['role']}")
    lines.append("  Either wire the fork into the image (pin it and build from "
                 "it) or stop carrying the patch. A fork that is tracked, "
                 "reported, and unreachable is the third state vibeic-eda#60 "
                 "refuses.")
    return 1, "\n".join(lines) + tail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    a = ap.parse_args(argv)
    rc, msg = report(Path(a.ledger))
    print(msg, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
