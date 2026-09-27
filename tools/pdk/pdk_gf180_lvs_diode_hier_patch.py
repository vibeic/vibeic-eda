#!/usr/bin/env python3
"""Extract gf180mcu's diode_mk junction diodes in the cell that draws them, so the
LVS runset's own run_mode=deep matches a clean design.

WHY
---
The gf180mcuD LibreLane config declares `KLAYOUT_LVS_OPTIONS run_mode deep`, and
the KLayout runset's diode extraction, exactly as the PDK ships it:

    extract_devices(diode('diode_nd2ps_06v0'),
                    { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell_con })
    extract_devices(diode('diode_pd2nw_06v0'),
                    { 'N' => diode_pd2nw_06v0_terminal_n, 'P' => diode_pd2nw_06v0_terminal_p })

KLayout forms a device from the connected cluster of its P and N input shapes,
and in deep mode it places the device in the cell that holds the whole cluster.
One side of these diodes is the WELL: `lvpwell_con` (nd2ps) or
`nwell_mv.not(dnwell).interacting(ncomp)` (pd2nw). A standard-cell row's well
touches the wells of every abutting cell, so the cluster spans the row and the
diode is extracted in the PARENT. MEASURED on spm (gf180mcuD, 60
gf180mcu_fd_sc_mcu7t5v0__antenna instances): deep extracts all 120 antenna
diodes into `spm` and leaves the antenna subcircuit with none, so the clean
design reports "Netlists don't match" (the same layout and schematic match in
run_mode=flat).

WHAT THIS WRITES  (three rule-deck files)
----------------
The well-side input of each of the eight diode_mk junction diodes becomes the
junction's own footprint on that well, a shape that lies in the diode's cell:

    diode_nd2ps_06v0_terminal_n_pwell = diode_nd2ps_06v0_terminal_n.and(lvpwell_con)
    diode_pd2nw_06v0_terminal_p_nwell = diode_pd2nw_06v0_terminal_p.and(diode_pd2nw_06v0_terminal_n)

and it is connected to the well (`lvpwell_con` / `nwell_con`), so the terminal
lands on the same net as before. The junction -- the overlap of the two inputs --
is the one the shipped deck extracts, so device classes, A and P are unchanged;
only the cell the device is extracted in changes. This is the form the ihp-sg13g2
runset uses for its antenna diodes (`dantenna_p = antenna_d_mk.and(pwell)...`,
`dpantenna_n = dpantenna_p.inside(nwell_drw)`), and it runs deep too.

The subject of each `and` is the diode's own shape. `lvpwell_con.and(diode_mk)`
-- the well as subject -- was measured to be hoisted as well (172 of 173 shapes
in the parent), because the neighbours' wells overlap the marker differently in
each placement.

MEASURED, same GDS and schematic (spm, gf180mcuD):

    deep, shipped deck   clean: don't match   120 diodes in spm, 0 in antenna
    deep, this patch     clean: match         0 in spm, 2 in the antenna cell
    deep, this patch     one instance removed from the schematic: don't match
    flat, either deck    clean: match, broken: don't match, the same 120 diodes

The other diodes (nw2ps, pw2dw, dw2ps, sc_diode) are not diode_mk junctions and
are not touched.

NOT FIXED UPSTREAM
------------------
globalfoundries-pdk-libs-gf180mcu_fd_pv (fossi-foundation) still extracts these
diodes with 'P' => lvpwell_con at 0776f8d9 (2026-08-25); its 417ae8e9 moved the
derivations into diode_extraction.lvs without changing the terminals. open_pdks
copies klayout/lvs verbatim, so the prebuilt volume is the only place this image
owns the bytes.

The anchors are exact text, each required to occur exactly once, and the caller
declares `--expect`, so on a deck that has moved this step FAILS the build rather
than applying somewhere else.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DECKS = "libs.tech/klayout/tech/lvs/rule_decks"
DERIVATIONS = f"{DECKS}/diode_derivations.lvs"
EXTRACTION = f"{DECKS}/diode_extraction.lvs"
CONNECTIONS = f"{DECKS}/diode_connections.lvs"

VARIANTS = ("03v3", "03v3_dn", "06v0", "06v0_dn")

# What "already patched" looks like, so a second application is refused.
MARK = "_terminal_n_pwell"

DERIVATIONS_ANCHOR = (
    "sc_diode_terminal_p = metal1_con.and(dnwell).not_interacting(sc_diode_terminal_n)"
    ".not(sc_diode_exclude)\n"
)

DERIVATIONS_BLOCK = """
# diode_mk junction diodes: the well side as the junction's own footprint.
# The well itself spans every abutting cell of a row, so with it as a device
# input the deep extractor puts the diode in the parent cell; the footprint lies
# in the cell that draws the diode. Connected to the well in
# diode_connections.lvs, so the terminal's net is unchanged.
""" + "".join(
    f"diode_nd2ps_{v}_terminal_n_pwell = diode_nd2ps_{v}_terminal_n.and(lvpwell_con)\n"
    f"diode_pd2nw_{v}_terminal_p_nwell = diode_pd2nw_{v}_terminal_p"
    f".and(diode_pd2nw_{v}_terminal_n)\n"
    for v in VARIANTS)

CONNECTIONS_ANCHOR = "connect(sc_diode_terminal_p, schottky_diode)\n"

CONNECTIONS_BLOCK = """
# diode_mk junction diodes: the well-side footprint joins the well
""" + "".join(
    f"connect(diode_nd2ps_{v}_terminal_n_pwell, lvpwell_con)\n"
    f"connect(diode_pd2nw_{v}_terminal_p_nwell, nwell_con)\n"
    for v in VARIANTS)


def _extraction_edits():
    """-> [(old, new)] exact text of every extract_devices call to rewrite."""
    edits = []
    for v in VARIANTS:
        base = v.replace("_dn", "")
        names = [f"diode_nd2ps_{v}"] if v == base else [f"diode_nd2ps_{v}", f"diode_nd2ps_{base}"]
        for name in names:
            indent = "" if v == base else "  "
            call = f"{indent}extract_devices(diode('{name}'), {{ 'N' => diode_nd2ps_{v}_terminal_n, "
            edits.append((call + "'P' => lvpwell_con })\n",
                          call + f"'P' => diode_nd2ps_{v}_terminal_n_pwell }})\n"))
    for v in VARIANTS:
        base = v.replace("_dn", "")
        if v == base:
            head = f"extract_devices(diode('diode_pd2nw_{v}'), "
            edits.append((
                head + f"{{ 'N' => diode_pd2nw_{v}_terminal_n, 'P' => diode_pd2nw_{v}_terminal_p }})\n",
                head + f"{{ 'N' => diode_pd2nw_{v}_terminal_p_nwell, 'P' => diode_pd2nw_{v}_terminal_p }})\n"))
        else:
            for name in (f"diode_pd2nw_{v}", f"diode_pd2nw_{base}"):
                head = f"  extract_devices(diode('{name}'),\n                  "
                edits.append((
                    head + f"{{ 'N' => diode_pd2nw_{v}_terminal_n, 'P' => diode_pd2nw_{v}_terminal_p }})\n",
                    head + f"{{ 'N' => diode_pd2nw_{v}_terminal_p_nwell, 'P' => diode_pd2nw_{v}_terminal_p }})\n"))
    return edits


EXTRACTION_EDITS = _extraction_edits()


def _append_after(src: str, anchor: str, block: str, path: str) -> str:
    n = src.count(anchor)
    if n != 1:
        raise ValueError(f"{path}: expected exactly 1 copy of the anchor, found {n}; "
                         f"anchor was:\n    {anchor.rstrip()}")
    return src.replace(anchor, anchor + block, 1)


def patch(root: Path):
    """-> ({path: new_text}, [change descriptions]); ValueError on a surprise."""
    files = {rel: root / rel for rel in (DERIVATIONS, EXTRACTION, CONNECTIONS)}
    for rel, path in files.items():
        if not path.is_file():
            raise ValueError(f"{path} does not exist; this is not a gf180mcu PDK root, "
                             f"or its LVS deck has moved")
    texts = {rel: path.read_text() for rel, path in files.items()}
    for rel, text in texts.items():
        if MARK in text:
            raise ValueError(f"{files[rel]}: already carries '{MARK}'. This step has run "
                             f"before, or the PDK now ships the correction itself.")

    out, changes = {}, []

    out[DERIVATIONS] = _append_after(texts[DERIVATIONS], DERIVATIONS_ANCHOR,
                                     DERIVATIONS_BLOCK, DERIVATIONS)
    changes.append(f"{DERIVATIONS}: {2 * len(VARIANTS)} well-side junction footprints defined")

    text = texts[EXTRACTION]
    for old, new in EXTRACTION_EDITS:
        n = text.count(old)
        if n != 1:
            raise ValueError(f"{EXTRACTION}: expected exactly 1 copy of an extract_devices "
                             f"call, found {n}; it was:\n{old}")
        text = text.replace(old, new, 1)
        changes.append(f"{EXTRACTION}: {new.split(chr(39))[1]} takes its well side "
                       f"from the junction footprint")
    out[EXTRACTION] = text

    out[CONNECTIONS] = _append_after(texts[CONNECTIONS], CONNECTIONS_ANCHOR,
                                     CONNECTIONS_BLOCK, CONNECTIONS)
    changes.append(f"{CONNECTIONS}: each footprint connected to its well")

    return {files[rel]: t for rel, t in out.items()}, changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything would change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changes; otherwise rc=2. A patch "
                         "step that silently stops matching would otherwise pass by "
                         "doing nothing.")
    args = ap.parse_args(argv)

    try:
        new, changes = patch(Path(args.root))
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

    for path, text in new.items():
        path.write_text(text)
        print(f"[written] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
