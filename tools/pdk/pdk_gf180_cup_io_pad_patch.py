#!/usr/bin/env python3
"""Make the gf180mcu CUP.2/CUP.3 bond-pad rules read the metal the DESIGN draws,
not the qualified pad metal the PDK's own IO masters carry.

WHY
---
`libs.tech/klayout/tech/drc/rule_decks/cup.rb`, exactly as the PDK ships it:

    cup2_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk)
                         .width(1.um).interacting(pad)
    cup3_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk)
                         .notch(1.um).interacting(pad)

`pad` is 37/0 and `metal_drawn` is the whole layout's metal, the foundry IO
library's included. Every pad-bearing `gf180mcu_fd_io` cell carries BOTH: its own
`Pad` opening AND its own slotted bond-pad metal inside that opening -- 0.3 um
slots on a 2 um pitch on Metal2/3/4. So each such cell, checked ALONE and
unmodified, reports 87 CUP.3 violations against its own qualified geometry, and
a die inherits 87 per bond pad (measured: 38 pads -> 3306 flat, 7 masters -> 609
deep) with the design's own fill, PDN, pad ring and seal ring contributing 0.

The deck's only escape is `not_interacting(guard_ring_mk)` (167/5), the
guard-ring/seal-ring STRUCTURE marker. Painting it over a bond pad removes the
87 and adds 762 GR.1/GR.2/GR.4 violations: it is not a per-pad exemption stamp.
Measured: no marker layer the IO library carries reaches the pad opening at all
(every marker in `gf180mcu_fd_io__bi_t` lies above y=68.7 um; the Pad opening is
y=2..62 um), so there is no marker to key an exemption on. The remaining
identity the PDK does offer is the CELL MASTER.

WHAT THIS REWRITES  (two edits, one file: rule_decks/cup.rb)
-------------------
1. `METAL_MAP_CUP` carries each level's SOURCE layer/datatype, and a new
   `CUP_IO_PAD_MASTER_CELLS = 'gf180mcu_fd_io__*'` states the exemption.
2. The rules' subject becomes the metal of that layer with the foundry IO pad
   masters (and everything under them) left unvisited:

       shapes = ctx.drc.source.cell_obj.begin_shapes_rec(
                  ctx.drc.source.layout.layer(layer, datatype))
       shapes.unselect_cells(CUP_IO_PAD_MASTER_CELLS)
       metal_drawn.data = RBA::Region.new(shapes).merge

   The iterator is over the deck's OWN layout, so no second source is created.
   That matters: `ctx.drc.source.select(...)` expresses the same filter in one
   line and was the first thing tried, but a second DRC source is a second deep
   shape store, and the whole deck then dies in an unrelated rule --
   `'nets': Internal error: deep shape store isn't singular` out of dnwell.rb's
   DN.1 -- as soon as it is run with `decks=all`, which is what LibreLane's own
   gf180 configuration runs. `decks=cup` alone does not show it.

`pad` and `guard_ring_mk` are NOT filtered: design metal that reaches into a
foundry cell's bond-pad opening -- the literal circuit-under-pad the rule exists
for -- is still the subject and is still checked.

NOT FIXED UPSTREAM
------------------
`fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv` at 035e6d1f (the tip
this was written against) ships `cup.rb` BYTE-IDENTICAL to the volume's copy
(md5 6a4e724731d04b657f9a96e73e45256b), and `git log` shows one touch ever
(f1b9021, "Upstream wafer.space changes"). There is no upstream correction to
take, and nothing in the deck keys on IO cells at all (grep: `lvs_io` appears in
esd.rb/sab.rb/geom.rb only). Unlike the PL.8 defect
(docs/DEFECT_gf180mcu_pl8_poly_dummy_density.md) advancing the PDK volume would
NOT bring a fix, because there is not one.

CORRECTION, NOT RELAXATION, AND THE DIFFERENCE IS MEASURED
----------------------------------------------------------
A relaxation makes a real violation stop being reported. Run through the deck
itself (tools/pdk/pdk_gf180_cup_check.py, which is a build step, and
fork-gatekeeper/test_pdk_gf180_cup_io_pad.py, which pins this program's
decisions):

    all 7 pad-bearing gf180mcu_fd_io masters, alone   87 CUP.3 -> 0
    the 7 masters with no Pad layer                    0       -> 0
    design-drawn 0.5 um notch on pad metal, no IO cell 1 CUP.3 -> 1 CUP.3
    design-drawn 0.5 um wide metal at a pad            1 CUP.2 -> 1 CUP.2
    IO master + design notch beside it                88      -> 1
    IO master + design notch INSIDE its Pad opening   88      -> 1
    IO master nested under a wrapper cell + notch     88      -> 1

If any of the last five rows ever reaches 0 the rules have been relaxed and this
patch is wrong.

The anchors below are exact whole lines, each required to occur exactly once,
and the caller declares `--expect 2`, so on a volume whose deck has moved this
step FAILS the build rather than applying somewhere else.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

CUP = "libs.tech/klayout/tech/drc/rule_decks/cup.rb"

# Anchors are whole source lines, matched exactly, required to occur EXACTLY
# ONCE. A near-miss is a different deck than the one this was written against.
MAP_ANCHOR = "METAL_MAP_CUP = {"

MAP_BLOCK_OLD = """METAL_MAP_CUP = {
  1 => { metal_drawn: 'metal1_drawn' },
  2 => { metal_drawn: 'metal2_drawn' },
  3 => { metal_drawn: 'metal3_drawn' },
  4 => { metal_drawn: 'metal4_drawn' },
  5 => { metal_drawn: 'metal5_drawn' },
  6 => { metal_drawn: 'metaltop_drawn' }
}.freeze
"""

MAP_BLOCK_NEW = """# Circuit-Under-Pad checks the metal the DESIGN draws at a bond pad.
#
# The foundry IO pad masters carry their own qualified bond-pad metal -- 0.3 um
# slots on a 2 um pitch -- inside their own Pad (37/0) opening, so each one,
# checked alone and exactly as the PDK ships it, reports 87 CUP.3 against its own
# geometry, and a die inherits 87 per bond pad. That metal is not the design's
# drawing. The deck's only other escape, not_interacting(guard_ring_mk), is the
# guard-ring STRUCTURE marker: stamping it over a pad trades 87 CUP.3 for 762
# GR.1/GR.2/GR.4. No marker layer the IO library carries reaches the pad opening,
# so the identity available is the cell master. See vibeic-eda#214.
#
# The Pad layer itself is NOT filtered: design metal reaching into a foundry
# cell's bond-pad opening is the literal circuit-under-pad this rule exists for
# and is still checked.
CUP_IO_PAD_MASTER_CELLS = 'gf180mcu_fd_io__*'

# Source layer/datatype per metal level, so the subject can be re-extracted with
# those masters unvisited rather than read from ctx (which holds the whole
# layout's metal, the IO masters' included).
METAL_MAP_CUP = {
  1 => { source: [34, 0] },
  2 => { source: [36, 0] },
  3 => { source: [42, 0] },
  4 => { source: [46, 0] },
  5 => { source: [81, 0] },
  6 => { source: [53, 0] }
}.freeze
"""

BODY_ANCHOR = "    metal_drawn = ctx[METAL_MAP_CUP[lvl][:metal_drawn]]"

BODY_REPLACEMENT = """    # Design metal only: the foundry IO pad masters, and everything under them,
    # are left unvisited. The iterator is over the deck's own layout, so no
    # second DRC source -- and no second deep shape store, which would break
    # every connectivity rule in the deck -- is created.
    layer, datatype = METAL_MAP_CUP[lvl][:source]
    shapes = ctx.drc.source.cell_obj.begin_shapes_rec(
      ctx.drc.source.layout.layer(layer, datatype)
    )
    shapes.unselect_cells(CUP_IO_PAD_MASTER_CELLS)
    design_metal = RBA::Region.new(shapes)
    design_metal.merge
    metal_drawn = ctx.drc.polygon_layer
    metal_drawn.data = design_metal"""

# What "already patched" looks like, so a second application is refused.
MARK = "CUP_IO_PAD_MASTER_CELLS"


def patch_cup(path: Path):
    """-> (new_text, [change descriptions]); raises ValueError on a surprise."""
    src = path.read_text()
    if MARK in src:
        raise ValueError(
            f"{path}: already carries '{MARK}'. This step has run before, or "
            f"the PDK now ships the correction itself.")

    if src.count(MAP_BLOCK_OLD) != 1:
        raise ValueError(
            f"{path}: expected exactly 1 copy of the METAL_MAP_CUP block this "
            f"step was written against, found {src.count(MAP_BLOCK_OLD)}. The "
            f"deck has moved.")

    lines = src.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == BODY_ANCHOR]
    if len(hits) != 1:
        raise ValueError(
            f"{path}: expected exactly 1 line equal to the rule-body anchor, "
            f"found {len(hits)}; anchor was:\n    {BODY_ANCHOR}")

    src = src.replace(MAP_BLOCK_OLD, MAP_BLOCK_NEW, 1)
    src = src.replace(BODY_ANCHOR + "\n", BODY_REPLACEMENT + "\n", 1)
    return src, [
        f"{CUP}: CUP_IO_PAD_MASTER_CELLS declared; METAL_MAP_CUP carries each "
        f"level's source layer/datatype",
        f"{CUP}: CUP.2/CUP.3 read design metal, the IO pad masters and "
        f"everything under them left unvisited",
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything would "
                         "change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changes; otherwise rc=2. A "
                         "patch step that silently stops matching would "
                         "otherwise pass by doing nothing.")
    args = ap.parse_args(argv)

    path = Path(args.root) / CUP
    if not path.is_file():
        print(f"FAIL: {path} does not exist. This is not a gf180mcu PDK root, "
              f"or its deck has moved.", file=sys.stderr)
        return 2
    try:
        new, changes = patch_cup(path)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    for c in changes:
        print(f"[patch]   {c}")

    if args.expect is not None and len(changes) != args.expect:
        print(f"FAIL: expected {args.expect} change(s), made {len(changes)}.",
              file=sys.stderr)
        return 2

    if args.check:
        return 1 if changes else 0

    path.write_text(new)
    print(f"[written] {path}: {len(changes)} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
