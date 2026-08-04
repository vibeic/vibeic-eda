#!/usr/bin/env python3
"""Grow every tech-LEF via patch that is narrower than its own routing layer's
declared minimum width, up to that minimum.

WHY
---
A tech LEF states both halves of a contradiction in the same file:

    LAYER met5
      TYPE ROUTING ;
      WIDTH 1.6 ;                                  <- the layer's own minimum

    VIA M4M5_PR DEFAULT
      LAYER via4 ; RECT -0.4  -0.4  0.4  0.4 ;
      LAYER met5 ; RECT -0.71 -0.71 0.71 0.71 ;    <- 1.42 um wide

The via patch is the metal a router drops at the TERMINUS of a route. While the
via sits inside a wire the union is the wire and nothing is wrong; where a wire
ENDS on the via, the part of the patch protruding past the wire end is 1.42 um
on a layer whose own minimum is 1.6 um, and sign-off DRC reports it (sky130
`m5.1`). A via patch narrower than min width can therefore never legally
terminate a route on that layer.

Nothing here is PDK- or via-name-specific: the corrected value is DERIVED from
the same file's `LAYER <l> TYPE ROUTING ... WIDTH w ;`. A file without the
contradiction is left byte-identical, and running it twice changes nothing.

WHAT IT REWRITES
----------------
1. `VIA <name> ... LAYER <routing> ; RECT x1 y1 x2 y2 ;`
   Grown symmetrically about the rect's own centre to at least the layer WIDTH
   on each axis.

2. `VIARULE <name> GENERATE ... LAYER <routing> ; ENCLOSURE ex ey ;`
   Raised so that `cut_extent + 2*enclosure >= WIDTH` on each axis, where the
   cut extent is the `LAYER <cut> ; RECT ...` in the same VIARULE. That is the
   single-cut, i.e. worst, case: a multi-cut array has a larger cut envelope,
   so the same enclosure covers it.

Values round UP to the manufacturing grid, so the result stays on grid.

The grown patch stays legal on the rule the original was presumably minimised
against — the via layer's own `ENCLOSURE ABOVE`, which states a MINIMUM. For
sky130 met5: 0.31 required, 0.40 produced.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

_NUM = r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?"

_LAYER_OPEN = re.compile(r"^\s*LAYER\s+(\S+)\s*$")
_BLOCK_CLOSE = re.compile(r"^\s*END\s+(\S+)\s*$")
_TYPE = re.compile(r"^\s*TYPE\s+(\S+)\s*;")
_WIDTH = re.compile(rf"^\s*WIDTH\s+({_NUM})\s*;")

_VIA_OPEN = re.compile(r"^\s*VIA\s+(\S+?)(?:\s+DEFAULT)?\s*$")
_VIARULE_OPEN = re.compile(r"^\s*VIARULE\s+(\S+)\s+GENERATE\s*(?:DEFAULT\s*)?$")
_SUBLAYER = re.compile(r"^(\s*)LAYER\s+(\S+)\s*;")
_RECT = re.compile(
    rf"^(\s*)RECT\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s*;(.*)$")
_ENCLOSURE = re.compile(rf"^(\s*)ENCLOSURE\s+({_NUM})\s+({_NUM})\s*;(.*)$")


def _fmt(v: float) -> str:
    """LEF-style number: no trailing zeros, no exponent."""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _ceil_grid(v: float, grid: float) -> float:
    return v if grid <= 0 else math.ceil(round(v / grid, 9)) * grid


def read_routing_widths(lines) -> dict:
    """The routing layers the file declares about itself -> min WIDTH."""
    widths = {}
    name = ltype = width = None
    for ln in lines:
        m = _LAYER_OPEN.match(ln)
        if m:
            name, ltype, width = m.group(1), None, None
            continue
        if name is None:
            continue
        m = _BLOCK_CLOSE.match(ln)
        if m and m.group(1) == name:
            if ltype == "ROUTING" and width is not None:
                widths[name] = width
            name = None
            continue
        m = _TYPE.match(ln)
        if m:
            ltype = m.group(1)
            continue
        m = _WIDTH.match(ln)
        if m and width is None:
            width = float(m.group(1))
    return widths


def patch(text: str, grid: float = 0.005, only=None):
    """-> (new_text, [human-readable change, ...]). Pure; no I/O."""
    lines = text.splitlines(keepends=True)
    routing = read_routing_widths(lines)
    if only:
        routing = {k: v for k, v in routing.items() if k in only}
    out = list(lines)
    changes = []

    i, n = 0, len(lines)
    while i < n:
        vm = _VIA_OPEN.match(lines[i])
        rm = _VIARULE_OPEN.match(lines[i])
        if not (vm or rm):
            i += 1
            continue
        name = (rm or vm).group(1)

        j = i + 1
        while j < n:
            e = _BLOCK_CLOSE.match(lines[j])
            if e and e.group(1) == name:
                break
            j += 1
        if j >= n:                                   # unterminated block
            i += 1
            continue

        cur = None
        rects, encs = {}, {}
        for k in range(i + 1, j):
            sm = _SUBLAYER.match(lines[k])
            if sm:
                cur = sm.group(2)
                continue
            if cur is None:
                continue
            r = _RECT.match(lines[k])
            if r:
                rects[cur] = (k, float(r.group(2)), float(r.group(3)),
                              float(r.group(4)), float(r.group(5)))
                continue
            e = _ENCLOSURE.match(lines[k])
            if e:
                encs[cur] = (k, float(e.group(2)), float(e.group(3)))

        if rm:
            # the cut extent = the RECT on the block's non-routing layer
            cut = None
            for lay, (_k, x1, y1, x2, y2) in rects.items():
                if lay not in routing:
                    cut = (x2 - x1, y2 - y1)
            for lay, (k, ex, ey) in encs.items():
                if lay not in routing or cut is None:
                    continue
                w = routing[lay]
                nx = max(ex, _ceil_grid((w - cut[0]) / 2.0, grid))
                ny = max(ey, _ceil_grid((w - cut[1]) / 2.0, grid))
                if nx <= ex + 1e-12 and ny <= ey + 1e-12:
                    continue
                m = _ENCLOSURE.match(lines[k])
                out[k] = (f"{m.group(1)}ENCLOSURE {_fmt(nx)} {_fmt(ny)} ;"
                          f"{m.group(4)}\n")
                changes.append(
                    f"VIARULE {name} LAYER {lay}: ENCLOSURE {_fmt(ex)} "
                    f"{_fmt(ey)} -> {_fmt(nx)} {_fmt(ny)} (cut "
                    f"{_fmt(cut[0])}x{_fmt(cut[1])} + 2*enc >= WIDTH {_fmt(w)})")
        else:
            for lay, (k, x1, y1, x2, y2) in rects.items():
                if lay not in routing:
                    continue
                w = routing[lay]
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                hx, hy = (x2 - x1) / 2.0, (y2 - y1) / 2.0
                nhx = max(hx, _ceil_grid(w / 2.0, grid))
                nhy = max(hy, _ceil_grid(w / 2.0, grid))
                if nhx <= hx + 1e-12 and nhy <= hy + 1e-12:
                    continue
                m = _RECT.match(lines[k])
                out[k] = (f"{m.group(1)}RECT {_fmt(cx - nhx)} {_fmt(cy - nhy)} "
                          f"{_fmt(cx + nhx)} {_fmt(cy + nhy)} ;{m.group(6)}\n")
                changes.append(
                    f"VIA {name} LAYER {lay}: patch {_fmt(2*hx)}x{_fmt(2*hy)}"
                    f" -> {_fmt(2*nhx)}x{_fmt(2*nhy)} (layer WIDTH {_fmt(w)})")

        i = j + 1

    return "".join(out), changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="tech LEF files")
    ap.add_argument("--layers", default="",
                    help="comma-separated routing layers to consider; "
                         "default = every routing layer the file declares")
    ap.add_argument("--grid", type=float, default=0.005,
                    help="manufacturing grid in um (default 0.005)")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything "
                         "would change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changes in total; "
                         "otherwise rc=2. A patch step that silently stops "
                         "matching would otherwise pass by doing nothing.")
    args = ap.parse_args(argv)

    only = {s.strip() for s in args.layers.split(",") if s.strip()} or None
    total = 0
    for f in args.files:
        p = Path(f)
        src = p.read_text()
        new, changes = patch(src, grid=args.grid, only=only)
        if not changes:
            print(f"[ok]      {p}: no via patch is narrower than its own layer")
            continue
        total += len(changes)
        for c in changes:
            print(f"[patch]   {p}: {c}")
        if not args.check:
            p.write_text(new)
            print(f"[written] {p}: {len(changes)} change(s)")

    if args.expect is not None and total != args.expect:
        print(f"FAIL: expected {args.expect} change(s), made {total}. The PDK "
              f"this ran against is not the one this step was written for.",
              file=sys.stderr)
        return 2
    return 1 if (args.check and total) else 0


if __name__ == "__main__":
    sys.exit(main())
