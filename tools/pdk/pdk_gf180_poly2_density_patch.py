#!/usr/bin/env python3
"""Make the gf180mcu PL.8 poly-density rule count the dummy poly the same PDK
writes.

WHY
---
The KLayout deck this PDK ships states both halves of a contradiction:

    drc/rule_decks/density.rb
        # Rule PL.8: Poly2 coverage over the entire die shall be 14%.
        ## Dummy poly2 lines must be added to meet the minimum poly2 density
        ## requirement.
        if (poly2.area / chip_area) * 100 < 14        <- poly2 is 30/0 ONLY

    drc/generic_layers.rb
        extract_single_layer_from_design.call(:poly2, 30, 0)
        extract_single_layer_from_design.call(:poly2_dummy, 30, 4)

    tech/scripts/fill_all.rb
        Poly2_Dummy = $ly.layer(30, 4)               <- what the fill writes
    tech/scripts/fill_poly2.rb
        fill_layer = Poly2_Dummy

So the rule tells the customer to add dummy poly to meet the floor, the PDK
ships the script that adds it, the script writes it on 30/4 -- and the rule
counts 30/0 alone, so the fill it asked for is invisible to it. A die that
meets the rule with the fill this PDK generated is reported as violating it.

The same deck already does the other thing for metal, in the same file:

    generic_layers.rb   register_numbered_metal_layers
        ctx.register_layer(names[:metal_result]) {
          ctx[names[:metal_drawn]] + ctx[names[:metal_dummy]] }

and density.rb's M1.4/M2.4/... read that union. Poly2 simply had no equivalent.

WHAT THIS REWRITES  (two lines of intent, in two files)
-------------------
1. generic_layers.rb  registers a derived layer

        { name: :poly2_result, calc: ->(ctx) { ctx[:poly2] + ctx[:poly2_dummy] } }

   in STATIC_LAYERS, the same `drawn + dummy` shape and the same file as the
   metal precedent above.

2. density.rb         PL.8 reads `poly2_result` instead of `poly2`.

FIXED UPSTREAM ALREADY
----------------------
fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv 2404d27b583b
(2026-07-04, "fix: ensure poly2 = poly2_drawn + poly2_dummy", 28 files). There
is no upstream patch to send; the PREBUILT ciel volume this image ships predates
it. Upstream took the opposite naming convention -- 30/0 was RENAMED to
:poly2_drawn, the union was given the old name :poly2 via a level-0 row in the
metal table, and 27 of the 28 files are that rename. That also hands the union to
every rule that still says bare poly2 (geom.rb's off-grid check, guard_ring.rb,
others), which moves more verdicts than PL.8 and is not measured here. This step
ports the PL.8 half only. See docs/DEFECT_gf180mcu_pl8_poly_dummy_density.md.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not redefine `:poly2`. Unlike metal, dummy poly carries its OWN rule
deck (rule_decks/dummy_poly2.rb, DPF.1-DPF.19), and DPF.5 is

        poly2_dummy.separation(poly2, 5.um, euclidian)

-- dummy poly measured AGAINST drawn poly. Folding the two into one symbol
would make that rule measure a layer against itself, and would silently change
device extraction (`tgate = poly2.and(comp)`) as well. The union is introduced
under a new name and consumed by the density rule alone, so every other rule
sees exactly the layer it saw before.

THIS IS A CORRECTION, NOT A RELAXATION, AND THE DIFFERENCE IS TESTABLE
----------------------------------------------------------------------
A relaxation makes a real violation stop being reported. Three generated dies
separate the two (tools/pdk/test_pdk_gf180_poly2_density_patch.py, and the
same three run through the deck itself):

    5% drawn + 15% PDK-style dummy   FAIL before -> PASS after   (the defect)
    5% drawn, no fill                FAIL before -> FAIL after   (the control)
    20% drawn, no fill               PASS before -> PASS after   (unchanged)

If the middle row ever goes green, the rule has been relaxed and this patch is
wrong.

The anchors below are exact whole lines and the caller declares `--expect 2`, so
on a volume that already carries upstream's fix this step FAILS the build rather
than applying to something else.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

GENERIC = "libs.tech/klayout/tech/drc/generic_layers.rb"
DENSITY = "libs.tech/klayout/tech/drc/rule_decks/density.rb"

# The anchors are whole source lines, matched exactly and required to occur
# EXACTLY ONCE. A near-miss is a different deck than the one this was written
# against, and guessing at it is how a patch step quietly applies to the wrong
# place.
GENERIC_ANCHOR = (
    "      { name: :tgate,           calc: ->(ctx) "
    "{ ctx[:poly2].and(ctx[:comp]).not(ctx[:res_mk]) } },"
)

GENERIC_INSERT = """
      # --- Poly2 as a DENSITY rule must count it ---
      # PL.8 asks for polysilicon coverage of the die and its own text tells the
      # customer to add dummy poly to reach it; this PDK's
      # tech/scripts/fill_poly2.rb writes that dummy on 30/4. The metal density
      # rules already read drawn + dummy (register_numbered_metal_layers,
      # below); poly2 had no such union, so the fill this PDK generates was
      # invisible to the rule that asked for it.
      #
      # DENSITY ONLY. :poly2 keeps its drawn-only meaning everywhere else: the
      # dummy has its own deck (rule_decks/dummy_poly2.rb) and DPF.5 measures
      # poly2_dummy SEPARATION FROM poly2, which folding the two together would
      # turn into a layer measured against itself.
      { name: :poly2_result,    calc: ->(ctx) { ctx[:poly2] + ctx[:poly2_dummy] } },
"""

DENSITY_ANCHOR = "  if (poly2.area / chip_area) * 100 < 14"

DENSITY_REPLACEMENT = """  # poly2_result = poly2 + poly2_dummy (generic_layers.rb). The dummy poly this
  # PDK's own tech/scripts/fill_poly2.rb writes on 30/4 is polysilicon on the
  # wafer and counts toward the coverage this rule tells the customer to add it
  # for -- the same drawn + dummy union the metal rules below already use.
  if (poly2_result.area / chip_area) * 100 < 14"""

# What "already patched" looks like, so a second application is refused rather
# than producing a deck whose Context#register_layer raises
# "Derived layer poly2_result already defined" at DRC time.
GENERIC_MARK = "{ name: :poly2_result,"
DENSITY_MARK = "poly2_result.area"


def _edit(path: Path, anchor: str, marker: str, apply_fn):
    """-> (new_text, [change lines]) ; raises ValueError on anything unexpected."""
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    if marker in src:
        raise ValueError(
            f"{path}: already carries '{marker}'. This step has run before, or "
            f"the PDK now ships the fix itself. Applying it twice would define "
            f"the derived layer twice, which the deck's own Context raises on.")
    hits = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == anchor]
    if len(hits) != 1:
        raise ValueError(
            f"{path}: expected exactly 1 line equal to the anchor, found "
            f"{len(hits)}. The deck this ran against is not the one this step "
            f"was written for; anchor was:\n    {anchor}")
    return apply_fn(lines, hits[0])


def patch_generic(path: Path):
    def apply_fn(lines, i):
        out = lines[:i + 1] + [GENERIC_INSERT.lstrip("\n")] + lines[i + 1:]
        return "".join(out), [
            f"{GENERIC}: registered derived layer :poly2_result = "
            f":poly2 + :poly2_dummy (after line {i + 1}, the :tgate entry)"]
    return _edit(path, GENERIC_ANCHOR, GENERIC_MARK, apply_fn)


def patch_density(path: Path):
    def apply_fn(lines, i):
        out = lines[:i] + [DENSITY_REPLACEMENT + "\n"] + lines[i + 1:]
        return "".join(out), [
            f"{DENSITY}: PL.8 counts poly2_result instead of poly2 "
            f"(line {i + 1})"]
    return _edit(path, DENSITY_ANCHOR, DENSITY_MARK, apply_fn)


TARGETS = ((GENERIC, patch_generic), (DENSITY, patch_density))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything "
                         "would change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changes in total; "
                         "otherwise rc=2. A patch step that silently stops "
                         "matching would otherwise pass by doing nothing.")
    args = ap.parse_args(argv)

    root = Path(args.root)
    total, pending = 0, []
    for rel, fn in TARGETS:
        p = root / rel
        if not p.is_file():
            print(f"FAIL: {p} does not exist. This is not a gf180mcu PDK root, "
                  f"or its deck has moved.", file=sys.stderr)
            return 2
        try:
            new, changes = fn(p)
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        total += len(changes)
        for c in changes:
            print(f"[patch]   {c}")
        pending.append((p, new, len(changes)))

    if args.expect is not None and total != args.expect:
        print(f"FAIL: expected {args.expect} change(s), made {total}. The PDK "
              f"this ran against is not the one this step was written for.",
              file=sys.stderr)
        return 2

    if args.check:
        return 1 if total else 0

    for p, new, n in pending:
        p.write_text(new)
        print(f"[written] {p}: {n} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
