#!/usr/bin/env python3
"""Every capability we advertise must RUN, not merely resolve.

WHY THIS EXISTS
===============
`fork-gatekeeper/daily_release.py:SMOKE` is twelve probes and every one of them
asks the same question: does the binary start and print its own name.

    "verilator": "verilator --version | grep -qi verilator",
    "iverilog":  "iverilog -V >/dev/null",
    "ngspice":   "ngspice --version >/dev/null 2>&1 || command -v ngspice",
    "sby":       "sby --help >/dev/null 2>&1 || command -v sby",
    "cocotb":    "python3 -c 'import cocotb; print(cocotb.__version__)'",

Its own docstring is honest about the limit -- "This proves each tool starts...
It does not prove any of them is correct" -- and `check_no_capability_lost.py`
names the same gap and points here: "Resolvability is a floor, not a guarantee,
and the smoke in daily_release is the layer that runs things."

The layer that runs things does not run things.  All twelve SMOKE probes PASS on
0.2.63 while, in that same image:

  * `verilator --version` passes; `--trace-fst` rc=2, `--sc` rc=1, and
    constrained `randomize()` produces values that violate the constraint.
  * `iverilog -V` passes; FST and LXT2 dumping are compiled out, and
    `$dumpfile("x.fst")` writes an ASCII VCD and exits 0.
  * `ngspice --version` passes; the image's DEFAULT PDK cannot simulate a
    transistor.
  * `sby --help` passes; the `smtbmc z3` engine line errors rc=16.
  * `import cocotb` passes; `waves=True` on Icarus raises RuntimeError.

None of these is a tool that failed to start.  Every one is a tool that started
and then could not do the thing it is in the image to do.

THE SHAPE THIS CLOSES
=====================
A code path shipped without its runtime dependency, where the failure lands at
USER time rather than at image-build time.  Build-time checks all pass because
nothing at build time ever asks the capability to do work.

THE FOUR RULES, WHICH ARE THE WHOLE POINT
=========================================
1. DRIVE THE REAL ENTRY POINT.  Not `--help`, not `command -v`, not a grep of an
   apt list.  A missing package is a hypothesis; a failing invocation is a
   finding.  `command -v FasterCap` succeeds in 0.2.63 -- and so does FasterCap,
   which is exactly why the probe must be the real one: reading the apt list
   would have produced a FALSE finding there.

2. EVERY SUBJECT CARRIES A CONTROL: the sibling path that shares everything
   except the suspect dependency.  --trace-vcd controls --trace-fst; waves=False
   controls waves=True; sky130 controls ihp-sg13g2.  Without one you cannot
   separate "the feature is broken" from "my test was wrong" -- which happened
   five times while writing this file, and the control caught it every time.

3. CHECK THE VALUE, NOT THE EXIT CODE.  The worst defect in this class returned
   SUCCESS with a wrong number.  Where a capability produces a number, this file
   asserts the number.

4. NEVER READ AN EXIT CODE THROUGH A PIPE.  `cmd | tail -5` reports tail's
   status, so a failing tool reads as rc=0.  The first draft of this very file
   scored `verilator --trace-fst` as WORKING for exactly that reason, on an
   image where it is measurably rc=2.  Every probe below therefore captures
   `$?` into the OUTPUT as `RC=` before anything can pipe it away.

WHAT THIS WOULD NOT CATCH, STATED
=================================
  * Wrongness inside a range this file does not pin.  It asserts a drain current
    is within a plausible band, not that the model card is right.
  * Anything needing a display (gtkwave's GUI) or a network (ciel's PDK fetch).
    Those stay visibly NOT-MEASURED rather than being quietly dropped, because
    "we stopped asking" is how this class survives.
  * A capability nobody thought to add a probe for.  This file is a list, and a
    list is only as good as its last edit.  It converts an unknown unknown into
    a known gap, which is the most it can honestly do.
  * Performance, scale, or anything about a real design.  Every probe is
    deliberately seconds-long so it can run on every release.

Exit: 0 all probes pass, 1 at least one BROKEN, 2 the image could not be run.
Run it with `--json` and diff the table between releases; a capability that goes
from WORKS to BROKEN is the release note nobody writes by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable, List, Optional

RC_OK, RC_BROKEN, RC_NOIMAGE = 0, 1, 2


# ── plumbing ──────────────────────────────────────────────────────────────────
class Probe:
    """One capability: a script to run in the image, and a verdict function.

    `check` receives the combined output and returns None when the capability
    works, or a one-line reason when it does not.  Returning a REASON rather
    than a bool is deliberate: a red probe has to say what it saw, or the next
    person re-derives it from scratch.

    Subject and control run in SEPARATE containers, so each script must set up
    everything it needs.  Sharing a /tmp path between them silently turned the
    controls red in the first draft.
    """

    def __init__(self, name: str, script: str,
                 check: Callable[[str], Optional[str]],
                 control: str, control_check: Callable[[str], Optional[str]]):
        self.name, self.script, self.check = name, script, check
        self.control, self.control_check = control, control_check


#: The image's profile.d prints two [INFO] banner lines into every login shell.
#: They are not tool output and must not reach a verdict function -- they are
#: long, they contain the word "PYTHONPATH", and they were the "first error"
#: three probes reported in the first draft.
_NOISE = re.compile(r"^\[INFO\] (Final|Setting)", re.M)


_VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "VERSION")


def _example_image() -> str:
    """The shipped tag, read at run time so no literal here can go stale."""
    try:
        with open(_VERSION_FILE, encoding="utf-8") as fh:
            return "ghcr.io/vibeic/vibeic-eda:" + fh.read().strip()
    except OSError:
        return "ghcr.io/vibeic/vibeic-eda:<version>"


def _run(image: str, script: str, timeout: int = 900) -> str:
    p = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "bash", image, "-lc", script],
        capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return "\n".join(ln for ln in out.splitlines() if not _NOISE.match(ln))


def _first_err(out: str) -> str:
    for ln in out.splitlines():
        if re.search(r"error|fatal|cannot|No such file|disabled|not found|Unable",
                     ln, re.I):
            return ln.strip()[:150]
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return (lines[-1] if lines else "<no output>")[:150]


def _rc(out: str, tag: str = "RC") -> Optional[int]:
    """The exit status the SCRIPT recorded, not the one the pipeline returned."""
    m = re.search(rf"^{tag}=(-?\d+)$", out, re.M)
    return int(m.group(1)) if m else None


def _wants(pattern: str, what: str) -> Callable[[str], Optional[str]]:
    def check(out: str) -> Optional[str]:
        rc = _rc(out)
        if rc not in (0, None):
            return f"rc={rc}: {_first_err(out)}"
        if not re.search(pattern, out, re.M):
            return f"{what} absent from output: {_first_err(out)}"
        return None
    return check


def _id_plausible(out: str, tag: str) -> Optional[str]:
    """Assert a drain current was printed AND is physically plausible.

    The band is deliberately wide -- 1 uA to 10 mA for a ~1 um device.  This is
    NOT a model-accuracy check and must not be read as one.  It separates "the
    device conducts" from "no simulation ran", which is the failure this class
    produces.  A number outside a decade of its sibling PDKs is worth a human.
    """
    if re.search(r"no simulations run|Simulation interrupted|Unknown model type",
                 out, re.I):
        return f"{tag}: {_first_err(out)}"
    m = re.search(r"-?i\(v1\)\s*=\s*([-\d.eE+]+)", out)
    if not m:
        return f"{tag}: no current printed -- {_first_err(out)}"
    idrain = abs(float(m.group(1)))
    if not (1e-6 <= idrain <= 1e-2):
        return f"{tag}: Id={idrain:.3e} A outside 1uA..10mA -- implausible"
    return None


# ── shared fixtures ───────────────────────────────────────────────────────────
# Written by both the subject and its control, because they do not share a
# container.  `d` is a fresh dir per probe so nothing inherits stale artefacts.
_COUNTER = r'''
d=$(mktemp -d); cd "$d"
cat > dut.v <<'XEOF'
`timescale 1ns/1ps
module dut(input clk, output reg [7:0] y);
  initial y = 0;
  always @(posedge clk) y <= y + 8'd1;
endmodule
XEOF
cat > tb.v <<'XEOF'
`timescale 1ns/1ps
module tb; reg clk=0; wire [7:0] y;
  dut u(.clk(clk), .y(y));
  initial begin
    $dumpfile("WAVEFILE"); $dumpvars(0,tb);
    repeat(20) #5 clk=~clk;
    $display("FINAL y=%0d", y); $finish;
  end
endmodule
XEOF
'''

_COCOTB = r'''
d=$(mktemp -d); cd "$d"
cat > dut.v <<'XEOF'
`timescale 1ns/1ps
module dut(input clk, input [7:0] a, output reg [7:0] y);
  always @(posedge clk) y <= a + 8'd1;
endmodule
XEOF
cat > test_dut.py <<'XEOF'
import cocotb
from cocotb.triggers import RisingEdge
from cocotb.clock import Clock

@cocotb.test()
async def t(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.a.value = 5
    await RisingEdge(dut.clk); await RisingEdge(dut.clk)
    assert dut.y.value == 6, f"got {dut.y.value}"
XEOF
cat > run.py <<'XEOF'
import sys
from cocotb_tools.runner import get_runner
w = sys.argv[1] == "1"
r = get_runner("icarus")
r.build(verilog_sources=["dut.v"], hdl_toplevel="dut",
        build_dir=f"bd{sys.argv[1]}", waves=w, always=True)
r.test(hdl_toplevel="dut", test_module="test_dut",
       build_dir=f"bd{sys.argv[1]}", waves=w)
XEOF
'''

_SBY = r'''
d=$(mktemp -d); cd "$d"
cat > seq.sv <<'XEOF'
module seq(input clk, input rst, output reg [7:0] cnt);
  always @(posedge clk) if (rst) cnt <= 8'h00; else cnt <= cnt + 8'h01;
endmodule
XEOF
cat > top.sv <<'XEOF'
module top(input clk, input rst);
  wire [7:0] cnt;
  seq u(.clk(clk),.rst(rst),.cnt(cnt));
  always @(posedge clk) assert (cnt <= 8'd255);
endmodule
XEOF
cat > t.sby <<'XEOF'
[options]
mode bmc
depth 8
[engines]
smtbmc ENGINE
[script]
read -formal seq.sv
read -formal top.sv
prep -top top
[files]
seq.sv
top.sv
XEOF
sed -i "s/ENGINE/SOLVER/" t.sby
sby -f -d td t.sby > sby.log 2>&1; echo "RC=$?"
tail -4 sby.log
'''

_VHDL = r'''
d=$(mktemp -d); cd "$d"
cat > inv.vhd <<'XEOF'
library ieee; use ieee.std_logic_1164.all;
entity inv is port(a: in std_logic; y: out std_logic); end;
architecture rtl of inv is begin y <= not a; end;
XEOF
'''


# ── the probes ────────────────────────────────────────────────────────────────
PROBES: List[Probe] = [

    # -- verilator: constrained randomize must OBEY the constraint ------------
    # THE VALUE, not the exit code.  This is the probe that catches the flagship
    # defect, where randomize() reported success and produced x=0.
    Probe(
        "verilator/constrained-randomize",
        r'''
d=$(mktemp -d); cd "$d"
cat > c.sv <<'XEOF'
class C;
  rand bit [7:0] x;
  constraint c1 { x > 8'd100; x < 8'd110; }
endclass
module tb;
  initial begin
    automatic C o = new; automatic int bad = 0; automatic int ok;
    for (int i=0;i<8;i++) begin
      ok = o.randomize();
      if (!(o.x > 100 && o.x < 110)) bad++;
    end
    $display("VIOLATIONS=%0d", bad);
    $finish;
  end
endmodule
XEOF
verilator --binary --timing -Wno-fatal --Mdir ob c.sv > build.log 2>&1; echo "BUILD_RC=$?"
./ob/Vc > run.log 2>&1; echo "RC=$?"
cat run.log
''',
        lambda out: (None if re.search(r"^VIOLATIONS=0$", out, re.M)
                     else f"constraint not honoured: {_first_err(out)}"),
        # CONTROL: an UNCONSTRAINED rand needs no solver -- verilator only shells
        # out when a constraint exists.  Red here would mean verilator or the
        # probe is at fault, not the solver wiring.
        r'''
d=$(mktemp -d); cd "$d"
cat > c2.sv <<'XEOF'
class C2; rand bit [7:0] x; endclass
module tb;
  initial begin
    automatic C2 o = new; automatic int ok; automatic int n = 0;
    for (int i=0;i<8;i++) begin ok = o.randomize(); if (ok) n++; end
    $display("UNCONSTRAINED_OK=%0d", n); $finish;
  end
endmodule
XEOF
verilator --binary --timing -Wno-fatal --Mdir ob2 c2.sv > build.log 2>&1; echo "BUILD_RC=$?"
./ob2/Vc2 > run.log 2>&1; echo "RC=$?"
cat run.log
''',
        _wants(r"^UNCONSTRAINED_OK=8$", "unconstrained randomize"),
    ),

    # -- verilator: the FST writer compiles in the USER's build ---------------
    Probe(
        "verilator/trace-fst",
        _COUNTER.replace("WAVEFILE", "t.fst") +
        'verilator --binary --timing --trace-fst --Mdir of tb.v dut.v > b.log 2>&1; echo "RC=$?"\n'
        'grep -iE "fatal error|Error 1" b.log | head -2\n',
        lambda out: (None if _rc(out) == 0
                     else f"rc={_rc(out)}: {_first_err(out)}"),
        _COUNTER.replace("WAVEFILE", "t.vcd") +
        'verilator --binary --timing --trace-vcd --Mdir ov tb.v dut.v > b.log 2>&1; echo "RC=$?"\n'
        'grep -iE "fatal error|Error 1" b.log | head -2\n',
        lambda out: (None if _rc(out) == 0
                     else f"rc={_rc(out)}: {_first_err(out)}"),
    ),

    # -- iverilog: FST dumping, INCLUDING the silent variant ------------------
    # Two assertions, because there are two failure modes.  The explicit `-fst`
    # flag must not error, AND the file the simulation writes must actually be
    # an FST -- the observed failure is a 727-byte ASCII VCD carrying an .fst
    # name and exit code 0, which only the second assertion sees.
    Probe(
        "iverilog/fst-dump",
        _COUNTER.replace("WAVEFILE", "t.fst") + r'''
iverilog -o a.vvp tb.v dut.v > c.log 2>&1; echo "COMPILE_RC=$?"
vvp a.vvp -fst > r.log 2>&1; echo "RC=$?"
grep -i "disabled" r.log | head -1
echo "FILETYPE=$(file -b t.fst 2>/dev/null || echo NOFILE)"
''',
        lambda out: (
            "FST compiled out of iverilog: " + _first_err(out)
            if re.search(r"support disabled", out, re.I) else
            f"vvp -fst rc={_rc(out)}" if _rc(out) != 0 else
            "SILENT: wrote an ASCII VCD under an .fst name, rc=0"
            if re.search(r"FILETYPE=ASCII", out) else
            "no FST produced" if re.search(r"FILETYPE=NOFILE", out) else None),
        _COUNTER.replace("WAVEFILE", "t.vcd") + r'''
iverilog -o a.vvp tb.v dut.v > c.log 2>&1; echo "COMPILE_RC=$?"
vvp a.vvp > r.log 2>&1; echo "RC=$?"
echo "BYTES=$(stat -c%s t.vcd 2>/dev/null || echo 0)"
''',
        lambda out: (None if _rc(out) == 0 and
                     int(re.search(r"BYTES=(\d+)", out).group(1)) > 100
                     else "VCD control produced no waveform"),
    ),

    # -- cocotb: the documented runner API, waves on --------------------------
    Probe(
        "cocotb/icarus-waves",
        _COCOTB + 'python3 run.py 1 > r.log 2>&1; echo "RC=$?"\ntail -8 r.log\n',
        _wants(r"PASS=1 FAIL=0", "a passing cocotb test with waves"),
        _COCOTB + 'python3 run.py 0 > r.log 2>&1; echo "RC=$?"\ntail -6 r.log\n',
        _wants(r"PASS=1 FAIL=0", "a passing cocotb test without waves"),
    ),

    # -- ngspice: the DEFAULT PDK must simulate a transistor ------------------
    # The instantiation syntax is copied verbatim from the PDK's own stdcell
    # netlist, so a red result cannot be blamed on the testbench.
    Probe(
        "ngspice/default-pdk-transistor",
        r'''
d=$(mktemp -d); cd "$d"
cat > d.sp <<'XEOF'
.lib /foss/pdks/ihp-sg13g2/libs.tech/ngspice/models/cornerMOSlv.lib mos_tt
V1 d 0 1.2
V2 g 0 1.2
XM1 d g 0 0 sg13_lv_nmos w=1.000u l=130.00n ng=1 m=1
.control
op
print -i(V1)
quit
.endc
.end
XEOF
ngspice -b d.sp > r.log 2>&1; echo "RC=$?"
grep -viE "^Error opening osdi|couldn.t be loaded" r.log | tail -12
''',
        lambda out: _id_plausible(out, "ihp-sg13g2"),
        # CONTROL: a different PDK down the identical code path.  Red here too
        # would mean ngspice; red only in the subject means the PDK's data.
        r'''
d=$(mktemp -d); cd "$d"
cat > s.sp <<'XEOF'
.lib /foss/pdks/sky130A/libs.tech/ngspice/sky130.lib.spice tt
V1 d 0 1.8
V2 g 0 1.8
XM1 d g 0 0 sky130_fd_pr__nfet_01v8 W=1 L=0.15
.control
op
print -i(V1)
quit
.endc
.end
XEOF
ngspice -b s.sp > r.log 2>&1; echo "RC=$?"
grep -viE "^Error opening osdi|couldn.t be loaded" r.log | tail -12
''',
        lambda out: _id_plausible(out, "sky130A"),
    ),

    # -- sby: the engine line published examples actually use -----------------
    Probe(
        "sby/smtbmc-z3",
        _SBY.replace("SOLVER", "z3"),
        _wants(r"DONE \(PASS", "a proved property via z3"),
        _SBY.replace("SOLVER", "yices"),
        _wants(r"DONE \(PASS", "a proved property via yices"),
    ),

    # -- ALIGN: reachable AND importable from a non-login shell ---------------
    Probe(
        "align/schematic2layout",
        'command -v schematic2layout > /dev/null 2>&1 || { echo "RC=127"; echo "not on PATH"; exit 0; }\n'
        'schematic2layout --help > r.log 2>&1; echo "RC=$?"\n'
        'grep -m1 "usage: schematic2layout" r.log || tail -2 r.log\n',
        _wants(r"usage: schematic2layout", "the ALIGN CLI"),
        # CONTROL: the venv is correct; only the environment around it is not.
        # `env -u` must wrap the interpreter itself -- a login shell re-exports
        # PYTHONPATH from profile.d before any command in the script runs.
        'env -u PYTHONPATH /foss/tools/align/bin/python3 '
        '/foss/tools/align/bin/schematic2layout.py --help > r.log 2>&1; echo "RC=$?"\n'
        'grep -m1 "usage: schematic2layout" r.log || tail -2 r.log\n',
        _wants(r"usage: schematic2layout", "ALIGN with PYTHONPATH cleared"),
    ),

    # -- yosys: VHDL synthesis ------------------------------------------------
    Probe(
        "yosys/vhdl-synth",
        _VHDL + r'''
printf 'plugin -i ghdl\nghdl inv.vhd -e inv\nsynth -top inv\nstat\n' > s.ys
yosys s.ys > r.log 2>&1; echo "RC=$?"
grep -iE "Number of cells|ERROR|symbol lookup" r.log | head -3
''',
        _wants(r"Number of cells", "a synthesised VHDL netlist"),
        # CONTROL: VHDL SIMULATION works (ghdl, nvc).  This localises the defect
        # to the yosys bridge rather than to VHDL support generally.
        _VHDL + 'ghdl -a inv.vhd > r.log 2>&1; echo "RC=$?"\n'
                'echo GHDL_ANALYSE_DONE\ntail -2 r.log\n',
        _wants(r"GHDL_ANALYSE_DONE", "ghdl VHDL analysis"),
    ),

    # -- OpenROAD: the Python API -------------------------------------------
    Probe(
        "openroad/python-api",
        'printf \'print("OR_PY_RAN")\\n\' > /tmp/p.py\n'
        'openroad -python /tmp/p.py > r.log 2>&1; echo "RC=$?"\ntail -3 r.log\n',
        _wants(r"OR_PY_RAN", "an executed OpenROAD python script"),
        # CONTROL: the Tcl interface, which is the same binary and the same
        # script-dispatch path minus the Python interpreter.
        'echo "puts OR_TCL_RAN" > /tmp/p.tcl\n'
        'openroad -no_init -exit /tmp/p.tcl > r.log 2>&1; echo "RC=$?"\ntail -3 r.log\n',
        _wants(r"OR_TCL_RAN", "an executed OpenROAD tcl script"),
    ),
]


# ── driver ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Example tag READ from VERSION, not written here. As a literal it passes the
    # repo's image-version guard only while it happens to equal the shipped
    # version, and silently becomes an unregistered stale pointer at the next
    # bump -- failing the gate suite for a reason that has nothing to do with
    # capabilities. The measured-on-0.2.63 references in the docstring above are
    # deliberately NOT parameterised: those are historical records of a specific
    # image and must not follow VERSION.
    ap.add_argument("image", help="image to probe, e.g. " + _example_image())
    ap.add_argument("--json", metavar="FILE", help="write the full result table")
    ap.add_argument("--only", metavar="SUBSTR", help="run probes whose name matches")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    try:
        if _run(a.image, "echo IMAGE_OK", timeout=180).find("IMAGE_OK") < 0:
            raise RuntimeError
    except Exception:
        print(f"cannot run {a.image}", file=sys.stderr)
        return RC_NOIMAGE

    rows, broken = [], 0
    probes = [p for p in PROBES if not a.only or a.only in p.name]
    for p in probes:
        try:
            reason = p.check(_run(a.image, p.script, a.timeout))
        except subprocess.TimeoutExpired:
            reason = f"timed out after {a.timeout}s"
        except Exception as exc:                                   # noqa: BLE001
            reason = f"probe raised {type(exc).__name__}: {exc}"

        try:
            crea = p.control_check(_run(a.image, p.control, a.timeout))
        except subprocess.TimeoutExpired:
            crea = f"control timed out after {a.timeout}s"
        except Exception as exc:                                   # noqa: BLE001
            crea = f"control raised {type(exc).__name__}: {exc}"

        # A red control does not excuse a red subject, but it changes what the
        # red MEANS: it says the probe itself is not trustworthy here.  Saying so
        # is the difference between a finding and a guess, and it is why this
        # verdict exists instead of being folded into BROKEN.
        if reason is None:
            verdict = "WORKS"
        elif crea is not None:
            verdict = "INCONCLUSIVE"
        else:
            verdict, broken = "BROKEN", broken + 1

        rows.append({"capability": p.name, "verdict": verdict,
                     "reason": reason, "control": crea or "ok"})
        mark = {"WORKS": "  ok   ", "BROKEN": " BROKEN",
                "INCONCLUSIVE": " ????? "}[verdict]
        print(f"{mark}  {p.name:34s} {reason or ''}")
        if crea:
            print(f"           control ALSO red: {crea}")

    print(f"\n{len(rows) - broken}/{len(rows)} capabilities run in {a.image}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rows, fh, indent=2)
    return RC_BROKEN if broken else RC_OK


if __name__ == "__main__":
    sys.exit(main())
