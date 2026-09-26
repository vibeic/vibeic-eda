#!/usr/bin/env python3
"""State gf180mcu LibreLane's LAYERS_RC wire resistance in the unit OpenROAD
reads it in: ohm/um, not kohm/um.

WHY
---
`libs.tech/librelane/config.tcl` of the prebuilt volume (open_pdks b344c97e):

    dict set ::env(LAYERS_RC) "*" Metal2 res 3.85861E-04

That is the ORFS gf180 fit, which states res in kohm/um.  OpenROAD's
`set_layer_rc -resistance r` converts with `sta::resistance_ui_sta r /
sta::distance_ui_sta 1.0`, i.e. per micron in the command resistance unit, and
that unit is the `pulling_resistance_unit` of the first Liberty loaded
(LibreLane's corner.tcl sets kOhm, `read_liberty` resets it).  The default
standard cell library, gf180mcu_fd_sc_mcu7t5v0, declares `1ohm` in every corner
(9t5v0 too).  MEASURED on 0.3.79: `report_layer_rc` after LibreLane's set_rc
sequence stores Metal2 at 3.86e-4 ohm/um, while the tech LEF states RPERSQ
0.090 / WIDTH 0.28 = 0.321 ohm/um -- every wire-RC estimate (GRT-stage STA,
the resizer's wire model, PSM's per-layer R) used wire resistance ~1000x low.

WHAT THIS WRITES
----------------
The four Metal2-5 `res` lines, each an exact whole-line match of the volume's
text, become the same fit x1000 (ohm/um) -- the values of vibeic/open_pdks#2,
so a volume cut after that lands carries them and this step REFUSES instead of
multiplying twice.  `cap` (pF/um, already the Liberty's unit) and VIAS_R (ohm
per cut, already right for a 1ohm Liberty) are not touched.

The step also REFUSES unless the libraries that inherit these values read them
in ohm: the default STD_CELL_LIBRARY's first Liberty of every CELL_LIBS corner
must declare `1ohm`, and so must every other SCL installed under libs.ref whose
LibreLane config does not set its own LAYERS_RC.  Pad libraries do not count:
gf180mcu_fd_io's Liberty is `1kohm`, but LibreLane reads PAD_LIBS after the
cell libs of each corner, so a pad Liberty is never the first one loaded.
(The OSU gp9t3v3/gp12t3v3 Liberty is `1kohm`; their configs are in the volume
but their libs.ref is not, so they cannot be loaded -- vibeic/open_pdks#2 gives
them their own values.)

UPSTREAM
--------
vibeic/open_pdks#2 (supersedes #1).  The prebuilt ciel volume is the only place
this image owns the bytes; advancing the volume past that fix makes this step
fail the build, which is the signal to delete it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CONFIG = "libs.tech/librelane/config.tcl"

#: (the volume's line, the line it becomes) -- exact whole lines.
LINES = tuple(
    (f'dict set ::env(LAYERS_RC) "*" {layer} res {old}',
     f'dict set ::env(LAYERS_RC) "*" {layer} res {new}')
    for layer, old, new in (
        ("Metal2", "3.85861E-04", "0.385861"),
        ("Metal3", "2.06673E-04", "0.206673"),
        ("Metal4", "1.68609E-04", "0.168609"),
        ("Metal5", "7.92778E-05", "0.0792778"),
    )
)

_UNIT = re.compile(r"pulling_resistance_unit\s*:\s*\"?\s*([\d.]+)\s*(k|m)?ohm\s*\"?\s*;", re.I)


def liberty_ohms(path: Path) -> float:
    with open(path, errors="replace") as fh:
        head = fh.read(200_000)
    m = _UNIT.search(head)
    if not m:
        raise ValueError(f"{path}: no pulling_resistance_unit in its header")
    return float(m.group(1)) * {"k": 1e3, "m": 1e-3, "": 1.0}[(m.group(2) or "").lower()]


def default_scl(text: str) -> str:
    m = re.search(r"if \{ !\[info exist ::env\(STD_CELL_LIBRARY\)\] \} \{\s*"
                  r"set ::env\(STD_CELL_LIBRARY\) (\S+)\s*\}", text)
    if not m:
        raise ValueError("no default STD_CELL_LIBRARY in the shared config")
    return m.group(1)


def first_libs(text: str, var: str, root: Path, scl: str) -> dict:
    """{corner: first Liberty path} of `dict set ::env(<var>) <corner> "..."`."""
    out = {}
    for corner, value in re.findall(
            r'^dict set ::env\(' + var + r'\) (\S+) "((?:[^"\\]|\\.)*)"', text, re.M | re.S):
        paths = value.replace("\\\n", " ").replace(
            "$::env(PDK_ROOT)/$::env(PDK)", str(root)).replace(
            "$::env(STD_CELL_LIBRARY)", scl).split()
        out[corner] = Path(paths[0])
    return out


def check_inheritors(root: Path, text: str) -> list:
    """-> [description]; ValueError unless every inheriting SCL reads res in ohm."""
    notes = []
    scl = default_scl(text)
    libs = first_libs(text, "CELL_LIBS", root, scl)
    if not libs:
        raise ValueError("no CELL_LIBS in the shared config")
    for corner, lib in sorted(libs.items()):
        if not lib.is_file():
            raise ValueError(f"default {scl} corner {corner}: {lib} is not installed")
        ohms = liberty_ohms(lib)
        if ohms != 1.0:
            raise ValueError(f"default {scl} corner {corner}: {lib.name} declares "
                             f"{ohms:g} ohm, not 1ohm; ohm/um values would be wrong for it")
    notes.append(f"{scl} (default): {len(libs)} CELL_LIBS corner(s), first Liberty 1ohm")
    for cfg in sorted((root / "libs.tech/librelane").glob("*/config.tcl")):
        other = cfg.parent.name
        if other == scl or not (root / "libs.ref" / other / "lib").is_dir():
            continue
        own = cfg.read_text()
        sets_libs = re.search(r"^\s*(?:set|dict set) ::env\((?:LIB|CELL_LIBS)\)", own, re.M)
        if "_sc_" not in other and not sets_libs:
            # A pad library (gf180mcu_fd_io, gf180mcu_ocd_io): its Liberty is
            # PAD_LIBS, which LibreLane appends AFTER the cell libs of every
            # corner (toolbox.get_timing_files_categorized), so it never is
            # the first Liberty and does not set the unit.
            continue
        if re.search(r"^\s*set ::env\(LAYERS_RC\)", own, re.M):
            continue
        mine = first_libs(own, "LIB", root, other) or first_libs(text, "CELL_LIBS", root, other)
        present = [lib for _, lib in sorted(mine.items()) if lib.is_file()]
        for lib in present:
            if liberty_ohms(lib) != 1.0:
                raise ValueError(f"{other} inherits the shared LAYERS_RC but {lib.name} declares "
                                 f"{liberty_ohms(lib):g} ohm")
        if present:
            notes.append(f"{other}: inherits, {len(present)} corner(s), first Liberty 1ohm")
    return notes


def patch(text: str) -> tuple:
    """-> (new text, [change lines]); ValueError on anything but the exact old lines."""
    lines = text.splitlines(keepends=True)
    changes = []
    for old, new in LINES:
        if any(l.rstrip("\n") == new for l in lines):
            raise ValueError(f"already ohm/um: {new!r} is present -- this step has run, "
                             f"or the volume carries vibeic/open_pdks#2")
        hits = [i for i, l in enumerate(lines) if l.rstrip("\n") == old]
        if len(hits) != 1:
            raise ValueError(f"expected exactly one line {old!r}, found {len(hits)}")
        i = hits[0]
        lines[i] = new + ("\n" if lines[i].endswith("\n") else "")
        changes.append(f"line {i + 1}: {old.split(' res ')[1]} -> {new.split(' res ')[1]} "
                       f"({old.split()[4]}, kohm/um -> ohm/um)")
    return "".join(lines), changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything would change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changed lines; otherwise rc=2")
    args = ap.parse_args(argv)
    root = Path(args.root)
    path = root / CONFIG
    if not path.is_file():
        print(f"FAIL: {path} does not exist; not a gf180mcu PDK root", file=sys.stderr)
        return 2
    text = path.read_text()
    try:
        notes = check_inheritors(root, text)
        new, changes = patch(text)
    except ValueError as exc:
        print(f"FAIL: {CONFIG}: {exc}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"[unit]    {n}")
    for c in changes:
        print(f"[patch]   {CONFIG}: {c}")
    if args.expect is not None and len(changes) != args.expect:
        print(f"FAIL: expected {args.expect} change(s), made {len(changes)}", file=sys.stderr)
        return 2
    if args.check:
        return 1 if changes else 0
    path.write_text(new)
    print(f"[written] {path}: {len(changes)} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
