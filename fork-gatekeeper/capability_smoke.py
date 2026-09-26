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
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import progress_watchdog as W                                  # noqa: E402

RC_OK, RC_BROKEN, RC_NOIMAGE = 0, 1, 2
DEFAULT_STALL_AFTER = 300.0
_DOCKER_CONTROL_TIMEOUT = 15


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


@dataclass(frozen=True)
class ProbeExecution:
    """One container invocation, before a capability verdict is attempted.

    ``outcome`` is deliberately separate from the tool output.  A natural exit
    produced an answer which a Probe may judge; ``stalled`` and ``launch_error``
    produced no complete measurement and must never be converted into BROKEN.
    """

    output: str
    outcome: str
    rc: int

    @property
    def measurable(self) -> bool:
        return self.outcome == "natural"


@dataclass(frozen=True)
class _ProgressJournal:
    """A private child-to-parent proof that the inner supervisor is advancing.

    The outer capability gate must not use arbitrary child output as a lease:
    a chatty wedged tool could then keep itself alive.  It also cannot wait only
    for a completed arm, because a legitimately long arm has no total-runtime
    ceiling.  The trusted smoke process therefore appends one record whenever
    its *inner watchdog* takes a sample.  The gate observes only monotonically
    increasing journal bytes; the container never sees this host path.
    """

    path: Optional[Path] = None

    def advance(self, event: str) -> None:
        if self.path is None:
            return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(event + "\n")
        except OSError:
            # The parent treats a journal which cannot advance as stalled.  A
            # write failure is deliberately not a capability verdict.
            pass


def _container_cpu_probe(cid_file: Path, progress: _ProgressJournal):
    """Return a probe for CPU consumed by the container's host process tree.

    The ``docker run`` client is not the parent of work in the container.  Using
    the watchdog's default host-client probe would therefore call a quiet
    compiler stalled even while it was consuming CPU.  Docker exposes the
    container init's host pid; once available, the shared monotonic process-tree
    probe accounts for that pid and all transitive children.

    ``docker inspect`` has a short operational deadline.  It is only a liveness
    sensor: failure to read it contributes no progress and forms no capability
    verdict.
    """
    tree = None

    def sample(_proc) -> Optional[float]:
        nonlocal tree
        # This is a supervisor-domain event, not workload stdout.  It keeps the
        # outer lease aligned with the inner lease while the inner watchdog is
        # still sampling, including while it counts down a genuine stall.
        progress.advance("inner-watchdog-sample")
        if tree is not None:
            return tree.sample()
        try:
            cid = cid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not cid:
            return None
        try:
            got = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Pid}}", cid],
                capture_output=True, text=True,
                timeout=_DOCKER_CONTROL_TIMEOUT,
            )
            pid = int(got.stdout.strip()) if got.returncode == 0 else 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if pid <= 0:
            return None
        tree = W.HostProcessTreeCpuProbe(pid)
        return tree.sample()

    return sample


def _container_killer(cid_file: Path, progress: _ProgressJournal):
    """Kill the container as well as its docker client on a progress stall."""
    def kill(proc, reason: str) -> None:
        progress.advance("inner-cleanup-start")
        try:
            cid = cid_file.read_text(encoding="utf-8").strip()
        except OSError:
            cid = ""
        if cid:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", cid], capture_output=True,
                    timeout=_DOCKER_CONTROL_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        progress.advance("inner-container-cleaned")
        W.terminate_process_group(proc, reason)
        progress.advance("inner-cleanup-complete")

    return kill


def _run(image: str, script: str,
         stall_after: float = DEFAULT_STALL_AFTER,
         progress: Optional[_ProgressJournal] = None) -> ProbeExecution:
    """Run one probe with a renewable no-progress lease and no runtime cap."""
    journal = progress or _ProgressJournal()
    with tempfile.TemporaryDirectory(prefix="vibeic-capability-") as tmp:
        cid_file = Path(tmp) / "container.cid"  # docker requires it not to exist
        result = W.run_supervised(
            ["docker", "run", "--rm", "--cidfile", str(cid_file),
             "--entrypoint", "bash", image, "-lc", script],
            stall_grace_s=stall_after,
            cpu_probe=_container_cpu_probe(cid_file, journal),
            kill=_container_killer(cid_file, journal),
        )
    combined = (result.out or "") + (result.err or "")
    output = "\n".join(
        ln for ln in combined.splitlines() if not _NOISE.match(ln))
    return ProbeExecution(output=output, outcome=result.outcome, rc=result.rc)


def run_container_probe(image: str, script: str,
                        stall_after: float = DEFAULT_STALL_AFTER
                        ) -> ProbeExecution:
    """Public adapter for other gates that need the same Docker liveness.

    The probe includes the container's host process tree rather than only the
    quiet ``docker`` client and removes the container on a stall.  Its progress
    signals are operational liveness only; callers still need their own
    postcondition/verdict logic after a natural exit.
    """
    return _run(image, script, stall_after=stall_after)


def _evaluate(execution: ProbeExecution,
              check: Callable[[str], Optional[str]],
              label: str) -> tuple[Optional[str], bool]:
    """Return ``(reason, unmeasurable)`` for one subject/control arm."""
    if not execution.measurable:
        kind = "STALLED" if execution.outcome == "stalled" else "UNMEASURABLE"
        return (f"{kind} ({execution.outcome}): "
                f"{_first_err(execution.output)}"), True
    try:
        return check(execution.output), False
    except Exception as exc:                                      # noqa: BLE001
        # A broken evaluator says nothing about the image.  In particular it
        # must not become BROKEN merely because the sibling control happened to
        # pass; that would turn a harness defect into a capability finding.
        return (f"UNMEASURABLE ({label} evaluator raised "
                f"{type(exc).__name__}: {exc})"), True


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
_CACE = r"""
d=$(mktemp -d); cd "$d"; mkdir -p cace/templates netlist/schematic; cd cace
export PDK_ROOT=/foss/pdks PDK=ihp-sg13g2
cat > p.yaml <<'XEOF'
name: p
description: capability probe
PDK: ihp-sg13g2
cace_format: 5.2
authorship: {designer: probe, creation_date: 2026-09-26, license: Apache 2.0}
paths: {root: .., netlist: netlist, templates: cace/templates}
pins:
  vdd: {description: supply, type: power, direction: inout}
  vss: {description: ground, type: ground, direction: inout}
  dn: {description: drain, type: signal, direction: output}
default_conditions:
  corner: {description: corner, display: Corner, typical: mos_tt}
  temperature: {description: temperature, display: Temp, unit: C, typical: 27}
parameters:
  dc:
    description: drain voltage
    display: Vd
    spec:
      vd: {display: Vd, unit: V, minimum: {value: 0}, maximum: {value: 1.2}}
    tool:
      ngspice: {template: dc.spice, format: ascii, suffix: .data, variables: [vd]}
    conditions:
      corner: {enumerate: [CORNERS]}
XEOF
cat > templates/dc.spice <<'XEOF'
.lib cornerMOSlv.lib CACE{corner}
.include CACE{DUT_path}
v1 vdd 0 1.2
xdut vdd 0 dn p
.temp CACE{temperature}
.control
op
let vd = v(dn)
echo $&vd > CACE{simpath}/CACE{filename}_CACE{N}.data
.endc
.end
XEOF
cat > ../netlist/schematic/p.spice <<'XEOF'
.subckt p vdd vss dn
rd vdd dn 20k
xm dn g vss vss sg13_lv_nmos w=1u l=0.13u
vg g vss 0.6
.ends p
XEOF
"""

_COSIM = r"""
d=$(mktemp -d); cd "$d"
cat > t.v <<'XEOF'
`timescale 1us/100ns
module t(out);
  output reg out;
  initial begin out = 1'b1; $display("VLOG_COSIM_ALIVE"); end
endmodule
XEOF
iverilog -o t t.v > c.log 2>&1 || { echo "RC=90"; cat c.log; exit 0; }
cat > c.cir <<'XEOF'
* d_cosim probe
adut null [ q ] vlog
.model vlog d_cosim simulation="ivlng" sim_args=[ "t" ]
abr [ q ] [ a ] dac1
.model dac1 dac_bridge(out_low=0 out_high=1.2)
r1 a 0 1k
.control
tran 1u 20u
meas tran vhigh find v(a) at=10u
.endc
.end
XEOF
"""


def _xyce_agrees(out: str) -> Optional[str]:
    vals = dict(re.findall(r"^(MOS_REL|NB_REL|RES_REL|MULT4)=([-\d.eE+]+)$",
                           out, re.M))
    if len(vals) != 4:
        return f"no Xyce/ngspice comparison printed: {_first_err(out)}"
    v = {k: float(x) for k, x in vals.items()}
    if v["MOS_REL"] > 1e-2:
        return f"MOS Id differs {100 * v['MOS_REL']:.2f}% from ngspice"
    if v["NB_REL"] > 1e-2:
        return f"diode-connected node differs {100 * v['NB_REL']:.2f}% from ngspice"
    if v["RES_REL"] > 3e-2:
        return f"rppd current differs {100 * v['RES_REL']:.2f}% from ngspice"
    if abs(v["MULT4"] - 4) > 1e-2:
        return f"m=4 carries {v['MULT4']:.3f}x the m=1 current, not 4x"
    return None


def _cace_orders(out: str) -> Optional[str]:
    if _rc(out) != 0:
        return f"cace rc={_rc(out)}: {_first_err(out)}"
    if re.search(r"TODO: Implement|No netlist or schematic", out):
        return f".spice template refused: {_first_err(out)}"
    vd = dict(re.findall(r"^VD_(tt|ss|ff)=([-\d.eE+]+)\s*$", out, re.M))
    if len(vd) != 3:
        return f"not every corner produced a value: {vd}"
    tt, ss, ff = (float(vd[k]) for k in ("tt", "ss", "ff"))
    if not ss > tt > ff:
        return f"corner not substituted: Vd ss={ss} tt={tt} ff={ff}"
    return None


def _cosim_ok(out: str) -> Optional[str]:
    if _rc(out) != 0:
        return f"ngspice d_cosim rc={_rc(out)}: {_first_err(out)}"
    if not re.search(r"VLOG_COSIM_ALIVE", out):
        return f"the Verilog side never ran: {_first_err(out)}"
    m = re.search(r"^vhigh\s*=\s*([-\d.eE+]+)", out, re.M | re.I)
    if not m or abs(float(m.group(1)) - 1.2) > 1e-3:
        return f"dac_bridge output not 1.2 V: {m.group(1) if m else 'absent'}"
    return None


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

    # -- Xyce: the IHP ADMS plugins must make Xyce simulate IHP devices -------
    # A REGULATED output is the wrong agreement metric: an LDO pins V(out) to
    # its reference whatever the pass device does, so a broken PSP plugin or an
    # ignored m= still "agrees". These are open-loop, device-sensitive values:
    # Id of an nmos and a pmos at |Vgs| 0.6/1.2 V, a diode-connected bias node,
    # and the ratio of an m=4 device to its m=1 twin. Measured in 0.3.79 with the
    # plugins built as this image builds them: the MOS currents agree with
    # ngspice+OSDI to < 1e-6 relative (same PSP103 Verilog-A, two compilers), the
    # diode node to 0.24 %, and m=4/m=1 = 4 to 5e-7. The rppd (r3_cmc) value is
    # NOT held to 1 %: it differs by 1.07 % between the two model trees
    # (10307 vs 10417 ohm for w=0.5u l=20u), so it is only bounded at 3 %.
    Probe(
        "xyce/ihp-plugins-vs-ngspice",
        r"""
d=$(mktemp -d); cd "$d"
export PDK_ROOT=/foss/pdks PDK=ihp-sg13g2
T=/foss/pdks/ihp-sg13g2/libs.tech; PL=$T/xyce/plugins
cp $T/ngspice/.spiceinit .
body() {
cat <<XEOF
.lib $T/$1/models/cornerMOSlv.lib mos_tt
.lib $T/$1/models/cornerRES.lib res_typ
vdn dn 0 1.2
vg g 0 1.2
vgp gp 0 0
vsp sp 0 1.2
vd1 dn d1 0
vd4 dn d4 0
vp1 p1 0 0
vbb bb 0 1.2
xn1 d1 g 0 0 sg13_lv_nmos w=1u l=0.13u
xn4 d4 g 0 0 sg13_lv_nmos w=1u l=0.13u m=4
xp1 p1 gp sp sp sg13_lv_pmos w=1u l=0.13u
xrb bb nb 0 rppd w=0.5u l=20u
xnb nb nb 0 0 sg13_lv_nmos w=2u l=1u
XEOF
}
{ echo "* x"; body xyce; printf '.options device temp=27\n.dc vg 0.6 1.2 0.6\n.print dc format=csv v(g) i(vd1) i(vd4) i(vp1) v(nb) i(vbb)\n.end\n'; } > x.cir
{ echo "* n"; body ngspice; printf '.temp 27\n.control\ndc vg 0.6 1.2 0.6\nwrdata n.txt v(g) i(vd1) i(vd4) i(vp1) v(nb) i(vbb)\n.endc\n.end\n'; } > n.sp
Xyce -plugin $PL/Xyce_Plugin_PSP103_VA.so,$PL/Xyce_Plugin_r3_cmc.so,$PL/Xyce_Plugin_mosvar.so x.cir > x.log 2>&1; echo "XRC=$?"
ngspice -b n.sp > n.log 2>&1; echo "RC=$?"
grep -m1 -iE "Unrecognized|error" x.log
python3 - <<'XEOF'
import csv
r=list(csv.reader(open("x.cir.csv"))); x=[[float(c) for c in row] for row in r[1:] if row]
n=[[float(v) for v in l.split()][1::2] for l in open("n.txt") if l.strip()]
def rel(a,b): return abs(a-b)/abs(b)
mos=max(rel(a[i],b[i]) for a,b in zip(x,n) for i in (1,2,3))
print("MOS_REL=%.3e" % mos)
print("NB_REL=%.3e" % max(rel(a[4],b[4]) for a,b in zip(x,n)))
print("RES_REL=%.3e" % max(rel(a[5],b[5]) for a,b in zip(x,n)))
print("MULT4=%.6f" % (x[-1][2]/x[-1][1]))
XEOF
""",
        lambda out: (
            f"rc={_rc(out, 'XRC')}/{_rc(out)}: {_first_err(out)}"
            if _rc(out, "XRC") != 0 or _rc(out) != 0 else
            _xyce_agrees(out)),
        # CONTROL: the same Xyce binary on a deck with no Verilog-A device. Red
        # here means Xyce itself, not the plugins or the IHP model cards.
        r"""
d=$(mktemp -d); cd "$d"
printf '* div\nv1 a 0 1.2\nr1 a b 1k\nr2 b 0 1k\n.dc v1 1.2 1.2 0.1\n.print dc format=csv v(b)\n.end\n' > c.cir
Xyce c.cir > c.log 2>&1; echo "RC=$?"
echo "VB=$(tail -1 c.cir.csv | cut -d, -f2)"
""",
        lambda out: (None if _rc(out) == 0 and re.search(r"^VB=6\.0+e-01$", out, re.M)
                     else f"Xyce divider: {_first_err(out)}"),
    ),

    # -- CACE: a plain-SPICE template must be substituted and simulated -------
    # The base image's cace 2.9.0 and upstream 2.13.0 stop every .spice template
    # at err("TODO: Implement substitution for spice templates!"). Three corners;
    # the drain voltage of a resistor-loaded nmos must differ per corner and order
    # ss > tt > ff, which a literal, unsubstituted template cannot produce.
    Probe(
        "cace/spice-template",
        _CACE.replace("CORNERS", "mos_tt, mos_ss, mos_ff") + r"""
python3 -m cace p.yaml -j 3 > r.log 2>&1; echo "RC=$?"
grep -m1 -E "TODO: Implement|No netlist or schematic" r.log
for c in tt ss ff; do
  f=$(grep -l "corner: mos_$c" ../runs/*/parameters/dc/run_*/conditions.yaml | head -1)
  echo "VD_$c=$(cat "$(dirname "$f")"/*.data 2>/dev/null)"
done
""",
        _cace_orders,
        # CONTROL: the same project run by hand through ngspice, i.e. everything
        # except CACE's substitution. Red here means the deck or the PDK.
        _CACE.replace("CORNERS", "mos_tt") + r"""
sed -e "s#CACE{corner}#mos_tt#; s#CACE{DUT_path}#$PWD/../netlist/schematic/p.spice#; s#CACE{temperature}#27#; s#CACE{simpath}/CACE{filename}_CACE{N}.data#$PWD/ctl.data#" templates/dc.spice > ctl.sp
cp /foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit .
ngspice -b ctl.sp > c.log 2>&1; echo "RC=$?"
echo "VD_tt=$(cat ctl.data 2>/dev/null)"
""",
        _wants(r"^VD_tt=0\.\d+", "a hand-substituted drain voltage"),
    ),

    # -- ngspice d_cosim: the Icarus shim must load libvvp --------------------
    # iverilog is built with --enable-libvvp so ngspice's d_cosim/ivlng can run a
    # Verilog model inside a SPICE transient. The shim loads "libvvp" BY NAME when
    # a deck gives no lib_args, so this runs with LD_LIBRARY_PATH REMOVED: only
    # the image's ld.so.conf entry can satisfy it. Measured on 0.3.79 (no
    # libvvp): rc 134, "Icarus shim failed to load VVP library". The value check
    # is a dac_bridge'd Verilog output measured on the SPICE side.
    Probe(
        "ngspice/d-cosim-icarus",
        _COSIM + r"""
env -u LD_LIBRARY_PATH ngspice -b c.cir > r.log 2>&1; echo "RC=$?"
grep -m1 -E "VLOG_COSIM_ALIVE" r.log
grep -m1 -iE "^vhigh" r.log
grep -m1 -iE "cannot open shared|failed to load" r.log
""",
        _cosim_ok,
        # CONTROL: the same compiled program under plain vvp, same environment.
        # vvp links libvvp.so.1 through its RUNPATH, not through ld.so.conf.
        _COSIM + r"""
env -u LD_LIBRARY_PATH vvp t > r.log 2>&1; echo "RC=$?"
cat r.log | head -3
""",
        _wants(r"VLOG_COSIM_ALIVE", "the Verilog $display under vvp"),
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
    ap.add_argument(
        "--stall-after", type=float, default=DEFAULT_STALL_AFTER,
        help="mark one subject/control arm unmeasurable after this many seconds "
             "with no output or container CPU progress; this is not a total "
             "runtime limit")
    ap.add_argument(
        "--progress-file", type=Path, help=argparse.SUPPRESS,
    )
    a = ap.parse_args()
    if not math.isfinite(a.stall_after) or a.stall_after <= 0:
        ap.error("--stall-after must be a positive finite number")

    progress = _ProgressJournal(a.progress_file)
    progress.advance("smoke-start")

    preflight = _run(
        a.image, "echo IMAGE_OK", stall_after=a.stall_after,
        progress=progress,
    )
    if not preflight.measurable or "IMAGE_OK" not in preflight.output:
        why = (f"{preflight.outcome}: {_first_err(preflight.output)}"
               if not preflight.measurable else _first_err(preflight.output))
        print(f"cannot run {a.image} -- UNMEASURABLE: {why}", file=sys.stderr)
        return RC_NOIMAGE
    print(f"  progress  {'image/preflight':34s} "
          f"subject={preflight.outcome}", flush=True)

    rows, broken = [], 0
    probes = [p for p in PROBES if not a.only or a.only in p.name]
    for p in probes:
        subject = _run(a.image, p.script, a.stall_after, progress)
        reason, subject_unmeasurable = _evaluate(
            subject, p.check, "subject")
        # This line is a human-visible boundary.  Outer liveness comes from the
        # private inner-watchdog journal, not stdout, so child chatter cannot
        # impersonate progress.
        print(f"  progress  {p.name:34s} subject={subject.outcome}", flush=True)

        control = _run(a.image, p.control, a.stall_after, progress)
        crea, control_unmeasurable = _evaluate(
            control, p.control_check, "control")
        print(f"  progress  {p.name:34s} control={control.outcome}", flush=True)

        # A red control does not excuse a red subject, but it changes what the
        # red MEANS: it says the probe itself is not trustworthy here.  Saying so
        # is the difference between a finding and a guess, and it is why this
        # verdict exists instead of being folded into BROKEN.
        #
        # A STALLED arm is even narrower: elapsed time supplied no capability
        # answer at all.  It is therefore always INCONCLUSIVE, never BROKEN,
        # even when its sibling completed green.
        if subject_unmeasurable:
            verdict = "INCONCLUSIVE"
        elif reason is None:
            verdict = "WORKS"
        elif control_unmeasurable or crea is not None:
            verdict = "INCONCLUSIVE"
        else:
            verdict, broken = "BROKEN", broken + 1

        rows.append({"capability": p.name, "verdict": verdict,
                     "reason": reason, "control": crea or "ok",
                     "subject_outcome": subject.outcome,
                     "control_outcome": control.outcome})
        mark = {"WORKS": "  ok   ", "BROKEN": " BROKEN",
                "INCONCLUSIVE": " ????? "}[verdict]
        print(f"{mark}  {p.name:34s} {reason or ''}", flush=True)
        if crea:
            print(f"           control ALSO red: {crea}", flush=True)

    works = sum(1 for row in rows if row["verdict"] == "WORKS")
    inconclusive = sum(1 for row in rows
                       if row["verdict"] == "INCONCLUSIVE")
    print(f"\n{works}/{len(rows)} capabilities WORK in {a.image}; "
          f"{inconclusive} unmeasurable/inconclusive", flush=True)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rows, fh, indent=2)
    return RC_BROKEN if broken else RC_OK


if __name__ == "__main__":
    sys.exit(main())
