#!/usr/bin/env python3
"""Give the gf180mcu Xyce model tree the two 5 V device names its standard
cells call.

WHY
---
Every transistor of the gf180mcu 5 V standard-cell libraries is a call of
`nfet_05v0` or `pfet_05v0`:

    libs.ref/gf180mcu_fd_sc_mcu7t5v0/spice/gf180mcu_fd_sc_mcu7t5v0.spice
        X_i_0 ZN A1 net_0 VPW nfet_05v0 W=8.2e-07 L=6e-07       (2403 + 2405 calls)

The ngspice tree of the SAME volume defines both, in its `fets_mm` section, as
the statistical wrapper of the 6 V model with the 5 V cells' default W/L:

    libs.tech/ngspice/sm141064.ngspice
        .subckt nfet_05v0 d g s b w=1e-6 l=6e-7 ...
        m0 d g s b nfet_06v0 ...
        .subckt pfet_05v0 d g s b w=1e-5 l=5e-7 ...
        m0 d g s b pfet_06v0 ...

The Xyce tree defines neither -- a `.subckt` census of the two trees differs by
exactly these two names -- so no 5 V standard cell can be simulated in Xyce at
all: "Subcircuit NFET_05V0 has not been defined for instance X_I_1"
(Xyce 7.10, a nand2_1 deck; the patched tree simulates it). MEASURED on
ghcr.io/vibeic/vibeic-eda@sha256:93d88e9e91b62d47326b84e3b0df58489d7fda4e9eb00e864e60c15b55996f08
(0.3.79): vibe-ic's step-30 Xyce arm refuses LL_SPICE_DEVICE_UNMODELLED
(`no section under .../libs.tech/xyce defines ['nfet_05v0', 'pfet_05v0']`).

WHAT THIS WRITES
----------------
For each name the ngspice tree defines and the Xyce tree lacks, and ONLY when
the ngspice definition is a wrapper of a model whose own wrapper the Xyce tree
already has, a copy of the Xyce tree's OWN wrapper -- its Xyce syntax, its
device line, its statistical parameters -- under the new name, with the
header defaults the ngspice definition declares. It is inserted immediately
before that wrapper, in the same `.lib` section, the order the ngspice tree
uses. Both copies of the file are patched (`sm141064.xyce` and the
byte-identical `sm141064.spice` beside it).

Nothing is invented: the device line is Xyce's, the defaults are the PDK's,
and the step REFUSES if the ngspice wrapper's statistical parameters
(`par_vth`, `par_k`, `par_l`, `par_w`) differ from its model's -- then the
two are not the same wrapper and a copy would be a guess.

The anchors are exact and the caller declares `--expect 4`, so a volume that
ships the names itself FAILS the build rather than defining them twice.

UPSTREAM
--------
The Xyce models come unmodified from google/gf180mcu-pdk models/xyce via
open_pdks; the 05v0 wrappers exist there for ngspice only. The prebuilt ciel
volume is the only place this image owns the bytes.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NGSPICE = "libs.tech/ngspice/sm141064.ngspice"
XYCE = ("libs.tech/xyce/sm141064.xyce", "libs.tech/xyce/sm141064.spice")
#: (the name the cells call, the model its ngspice wrapper instantiates)
NAMES = (("nfet_05v0", "nfet_06v0"), ("pfet_05v0", "pfet_06v0"))
STATISTICAL = ("par_vth", "par_k", "par_l", "par_w")

_HEAD = re.compile(r"^\.subckt\s+(\S+)\s+d\s+g\s+s\s+b\s+(.*)$", re.I)


def blocks(text: str) -> dict:
    """{subckt name: (first line index, last line index, lines)} -- first wins."""
    lines = text.splitlines(keepends=True)
    out, start = {}, None
    for i, line in enumerate(lines):
        m = _HEAD.match(line.rstrip("\n"))
        if m:
            start = (m.group(1), i)
        elif start and line.lower().startswith(".ends"):
            name, first = start
            out.setdefault(name, (first, i, lines[first:i + 1]))
            start = None
    return out


def params(line: str) -> dict:
    return dict(p.split("=", 1) for p in _HEAD.match(line.rstrip("\n")).group(2).split())


def statistical(body: list) -> dict:
    out = {}
    for line in body:
        m = re.match(r"^\.param\s+(\w+)\s*=\s*'?([^'\s]+)'?\s*$", line.strip())
        if m and m.group(1) in STATISTICAL:
            out[m.group(1)] = float(m.group(2))
    return out


def port(ngspice_text: str, xyce_text: str) -> tuple:
    """-> (new xyce text, [change lines]); ValueError on anything unexpected."""
    ng, xy = blocks(ngspice_text), blocks(xyce_text)
    lines = xyce_text.splitlines(keepends=True)
    inserts, changes = [], []
    for name, model in NAMES:
        if name in xy:
            raise ValueError(f"the Xyce tree already defines {name}: this step has run, "
                             f"or the PDK now ships it")
        if name not in ng or model not in ng or model not in xy:
            raise ValueError(f"expected ngspice {name} and {model} and Xyce {model}; "
                             f"found ngspice {sorted(set(ng) & {name, model})}, "
                             f"Xyce {sorted(set(xy) & {model})}")
        body = ng[name][2]
        calls = [l for l in body if re.match(r"^\s*m\S*\s", l, re.I)]
        if len(calls) != 1 or calls[0].split()[5] != model:
            raise ValueError(f"ngspice {name} is not a one-device wrapper of {model}")
        if statistical(body) != statistical(ng[model][2]) or \
                statistical(ng[model][2]) != statistical(xy[model][2]):
            raise ValueError(f"{name}'s statistical parameters are not {model}'s: "
                             f"{statistical(body)} vs {statistical(xy[model][2])}")
        defaults, header = params(body[0]), params(xy[model][2][0])
        if set(defaults) != set(header):
            raise ValueError(f"{name} declares {sorted(defaults)}, Xyce {model} "
                             f"{sorted(header)}")
        first, _, xy_body = xy[model]
        head = f".subckt {name} d g s b " + " ".join(f"{k}={defaults[k]}" for k in header)
        new = [head + "\n"] + xy_body[1:-1] + [f".ends {name}\n"]
        inserts.append((first, new))
        changes.append(f"{name}: Xyce's own {model} wrapper, ngspice's defaults "
                       f"({' '.join(f'{k}={defaults[k]}' for k in ('w', 'l'))}), "
                       f"before line {first + 1}")
    for first, new in sorted(inserts, reverse=True):
        lines[first:first] = new
    return "".join(lines), changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="the PDK root, e.g. /foss/pdks/gf180mcuD")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; rc=1 if anything would change")
    ap.add_argument("--expect", type=int, default=None,
                    help="require exactly this many changes in total; otherwise rc=2")
    args = ap.parse_args(argv)
    root = Path(args.root)
    ng = root / NGSPICE
    if not ng.is_file():
        print(f"FAIL: {ng} does not exist; not a gf180mcu PDK root", file=sys.stderr)
        return 2
    total, pending = 0, []
    for rel in XYCE:
        path = root / rel
        if not path.is_file():
            print(f"FAIL: {path} does not exist", file=sys.stderr)
            return 2
        try:
            new, changes = port(ng.read_text(), path.read_text())
        except ValueError as exc:
            print(f"FAIL: {rel}: {exc}", file=sys.stderr)
            return 2
        total += len(changes)
        for c in changes:
            print(f"[patch]   {rel}: {c}")
        pending.append((path, new, len(changes)))
    if args.expect is not None and total != args.expect:
        print(f"FAIL: expected {args.expect} change(s), made {total}", file=sys.stderr)
        return 2
    if args.check:
        return 1 if total else 0
    for path, new, n in pending:
        path.write_text(new)
        print(f"[written] {path}: {n} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
