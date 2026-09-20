#!/usr/bin/env python3
"""Both directions of the gf180mcu CUP bond-pad correction, run through the deck
itself (vibeic-eda#214).

The correction in `pdk_gf180_cup_io_pad_patch.py` stops CUP.2/CUP.3 reporting the
foundry IO pad masters' own qualified, slotted bond-pad metal. A relaxation would
also stop reporting the metal a DESIGN draws at a pad, which is what these rules
exist for. Only running the deck separates the two, so this runs it:

  POSITIVE (must be silent)  every top cell of the shipped
      libs.ref/gf180mcu_fd_io GDS that carries a Pad (37/0) layer, extracted into
      a GDS of its own and checked ALONE: 0 CUP.2 and 0 CUP.3.

  NEGATIVE (must still fire) three synthetic tops, none of whose metal comes from
      an IO master:
        notch   a design-drawn metal1 polygon with a 0.5 um notch under a
                design-drawn Pad opening              -> >= 1 CUP.3
        narrow  a design-drawn 0.5 um wide metal1 line under the same
                                                      -> >= 1 CUP.2
        under   an IO master instantiated, plus a design-drawn notched polygon
                INSIDE that master's own Pad opening -- the literal
                circuit-under-pad                     -> exactly 1 CUP.3, i.e.
                                                         the design's own and
                                                         none of the master's 87

Nothing here is copied from any design: the fixtures are generated from
rectangles, and the only PDK input is the IO library the image ships.

rc 0 all directions as stated; rc 1 a verdict moved the wrong way (each is named
on stderr); rc 2 could not measure -- a missing PDK, IO GDS, klayout or deck.
"Could not read it" is never reported as "read it and it was clean".
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DECK = "libs.tech/klayout/tech/drc/gf180mcu.drc"
IO_GDS = "libs.ref/gf180mcu_fd_io/gds/gf180mcu_fd_io.gds"
PAD_LAYER = (37, 0)
METAL1 = (34, 0)

# (x, y) um of a 20x20 um polygon carrying one 0.5 um notch 15 um deep.
NOTCH_PTS = [(0, 0), (20, 0), (20, 20), (10.25, 20),
             (10.25, 5), (9.75, 5), (9.75, 20), (0, 20)]

_COUNT = re.compile(r"'(CUP\.[0-9]+)'\s*:\s*([0-9]+)")


def _counts(log: str) -> dict:
    return {m.group(1): int(m.group(2)) for m in _COUNT.finditer(log)}


def _run_deck(deck: Path, gds: Path, topcell: str, workdir: Path) -> dict:
    out = subprocess.run(
        ["klayout", "-b", "-r", str(deck),
         "-rd", f"input={gds}",
         "-rd", f"report={workdir / (topcell + '.lyrdb')}",
         "-rd", f"topcell={topcell}",
         "-rd", "decks=cup"],
        capture_output=True, text=True, cwd=str(workdir))
    log = out.stdout + out.stderr
    if "DRC RESULT" not in log:
        raise RuntimeError(
            f"klayout produced no DRC verdict for {topcell} (rc={out.returncode}). "
            f"Last lines:\n" + "\n".join(log.strip().splitlines()[-8:]))
    return _counts(log)


def _build_fixtures(pya, io_gds: Path, workdir: Path):
    """-> (pad_cells, nopad_cells, synthetic) as lists of (gds, topcell)."""
    src = pya.Layout()
    src.read(str(io_gds))
    pad_idx = src.find_layer(*PAD_LAYER)
    pad_cells, nopad_cells = [], []
    first_pad_cell = None
    for cell in src.top_cells():
        if cell.name.startswith("$") or cell.name == "(UNNAMED)":
            continue
        has_pad = False
        if pad_idx is not None:
            has_pad = pya.Region(cell.begin_shapes_rec(pad_idx)).count() > 0
        out = pya.Layout()
        out.dbu = src.dbu
        top = out.create_cell(cell.name)
        top.copy_tree(cell)
        gds = workdir / f"{cell.name}.gds"
        out.write(str(gds))
        (pad_cells if has_pad else nopad_cells).append((gds, cell.name))
        if has_pad and first_pad_cell is None:
            first_pad_cell = cell.name

    def um(ly, v):
        return int(round(v / ly.dbu))

    def notch(ly, x0, y0):
        return pya.Polygon([pya.Point(um(ly, x0 + x), um(ly, y0 + y))
                            for x, y in NOTCH_PTS])

    synthetic = []

    ly = pya.Layout(); ly.dbu = src.dbu
    top = ly.create_cell("CUP_NOTCH_DESIGN_ONLY")
    top.shapes(ly.layer(*METAL1)).insert(notch(ly, 0, 0))
    top.shapes(ly.layer(*PAD_LAYER)).insert(pya.Box(0, 0, um(ly, 20), um(ly, 20)))
    ly.write(str(workdir / "cup_notch.gds"))
    synthetic.append((workdir / "cup_notch.gds", "CUP_NOTCH_DESIGN_ONLY",
                      "CUP.3", "notch"))

    ly = pya.Layout(); ly.dbu = src.dbu
    top = ly.create_cell("CUP_NARROW_DESIGN_ONLY")
    top.shapes(ly.layer(*METAL1)).insert(
        pya.Box(0, 0, um(ly, 20), um(ly, 0.5)))
    top.shapes(ly.layer(*PAD_LAYER)).insert(pya.Box(0, 0, um(ly, 20), um(ly, 20)))
    ly.write(str(workdir / "cup_narrow.gds"))
    synthetic.append((workdir / "cup_narrow.gds", "CUP_NARROW_DESIGN_ONLY",
                      "CUP.2", "narrow"))

    under = None
    if first_pad_cell is not None:
        ly = pya.Layout(); ly.dbu = src.dbu
        master = ly.create_cell(first_pad_cell)
        master.copy_tree(src.cell(first_pad_cell))
        top = ly.create_cell("CUP_UNDER_PAD")
        top.insert(pya.CellInstArray(master.cell_index(), pya.Trans()))
        # inside the master's own Pad opening, which starts at (7.5, 2) um
        top.shapes(ly.layer(*METAL1)).insert(notch(ly, 20, 20))
        ly.write(str(workdir / "cup_under_pad.gds"))
        under = (workdir / "cup_under_pad.gds", "CUP_UNDER_PAD", first_pad_cell)
    return pad_cells, nopad_cells, synthetic, under


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--workdir", default=None,
                    help="where to build the fixtures (default: a temp dir, "
                         "removed afterwards)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    deck, io_gds = root / DECK, root / IO_GDS
    for p in (deck, io_gds):
        if not p.is_file():
            print(f"NOT_MEASURED: {p} does not exist", file=sys.stderr)
            return 2
    try:
        import pya  # noqa: F401  (the klayout python module in the image)
    except ImportError as exc:
        print(f"NOT_MEASURED: the klayout python module is unavailable: {exc}",
              file=sys.stderr)
        return 2
    import pya

    tmp = None
    if args.workdir:
        workdir = Path(args.workdir); workdir.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="cupcheck.")
        workdir = Path(tmp.name)

    failures = []
    try:
        pad_cells, nopad_cells, synthetic, under = _build_fixtures(
            pya, io_gds, workdir)
        if not pad_cells:
            print("NOT_MEASURED: no top cell in the shipped IO GDS carries a "
                  "Pad (37/0) layer; this is not the library this check was "
                  "written for", file=sys.stderr)
            return 2

        for gds, name in pad_cells:
            c = _run_deck(deck, gds, name, workdir)
            tot = c.get("CUP.2", 0) + c.get("CUP.3", 0)
            print(f"[cup] POSITIVE {name:32s} CUP.2={c.get('CUP.2', 0)} "
                  f"CUP.3={c.get('CUP.3', 0)}")
            if tot:
                failures.append(
                    f"{name}: the PDK's own pad master still reports {tot} "
                    f"CUP violation(s) against its own geometry")

        for gds, name in nopad_cells:
            c = _run_deck(deck, gds, name, workdir)
            print(f"[cup] no-pad   {name:32s} CUP.2={c.get('CUP.2', 0)} "
                  f"CUP.3={c.get('CUP.3', 0)}  (rule not applicable)")

        for gds, name, rule, label in synthetic:
            c = _run_deck(deck, gds, name, workdir)
            n = c.get(rule, 0)
            print(f"[cup] NEGATIVE {label:10s} {rule}={n} (must be >= 1)")
            if n < 1:
                failures.append(
                    f"{label}: a design-drawn violation at a bond pad is no "
                    f"longer reported -- {rule} went silent. That is a "
                    f"RELAXATION, not the correction.")

        if under is None:
            print("NOT_MEASURED: circuit-under-pad arm not built", file=sys.stderr)
            return 2
        gds, name, master = under
        c = _run_deck(deck, gds, name, workdir)
        n = c.get("CUP.3", 0)
        print(f"[cup] NEGATIVE under-pad  CUP.3={n} (must be exactly 1: the "
              f"design's own notch inside {master}'s Pad opening, and none of "
              f"the master's own)")
        if n != 1:
            failures.append(
                f"under-pad: expected exactly 1 CUP.3 (the design's), measured "
                f"{n}. 0 means the rule no longer sees design metal over a pad; "
                f">1 means the master's own geometry is being reported again.")
    except RuntimeError as exc:
        print(f"NOT_MEASURED: {exc}", file=sys.stderr)
        return 2
    finally:
        if tmp is not None:
            tmp.cleanup()

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print(f"gf180mcuD CUP: {len(pad_cells)} shipped pad master(s) silent, "
          f"{len(synthetic) + 1} design-drawn violation(s) still reported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
