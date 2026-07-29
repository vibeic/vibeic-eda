#!/usr/bin/env python3
"""Does the tool the flow INVOKES come from the artefact we built?

WHY THIS EXISTS (vibeic-eda#17, #18)
====================================
Every gate in the delivery chain stops one hop short of the question that matters.

    check_pins_agree      the commit is stated identically in all three places
    check_pins_current    the pin is the tip of its fork branch
    daily_release         the artefact for that pin exists and is published
    the smoke             the tool starts and prints a version

All four pass while the flow runs a different binary. Measured in 0.2.31:

  * `verilator` resolves to /foss/tools/verilator/bin/verilator — the BASE
    image's 5.048 from April. Our build is a complete, working 5.051 at
    /foss/tools/verilator-vibeic, referenced by nothing at all (#18). Two
    `V3Randomize` constraint-solver commits ship and never run.
  * `klayout` resolves to the base image's June build; our fork's LEF/DEF plugin
    — the one that honours tech-LEF MANUFACTURINGGRID — is loaded only by
    `svrfdrc` (#17).

"The image contains our tool" and "the flow runs our tool" are different claims,
and only the first was ever checked.

HOW IT DECIDES, WITHOUT TRUSTING A VERSION STRING
=================================================
The root Dockerfile already declares which paths came from our artefacts:

    COPY --from=img-verilator /foss/tools/verilator-vibeic /foss/tools/verilator-vibeic

So the check is a containment test: resolve what the flow would invoke, and ask
whether that realpath lies under a path we copied from our own build. No version
parsing — `verilator --version` printed 5.048 for months and nothing noticed,
because a version string answers "which build is this?" only if you already know
which builds exist.

WHAT IT CANNOT SEE, STATED
==========================
A tool that is ours by path but built from the wrong commit. Path containment
proves origin, not currency; `check_pins_current` covers that hop, and the two
together are what the chain needs. It also cannot see a library loaded at
runtime from outside the resolved binary's directory — #17 is exactly that
shape, and it is caught here only because klayout's BINARY is also not ours.

Exit: 0 every tool resolves into our build, 1 one or more does not, 2 nothing
checked. Report-only by default (`--strict` to fail): #17 and #18 are open, so a
gate that blocks the daily release on them would be switched off within a day.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

RC_OK, RC_NOT_OURS, RC_NOTHING = 0, 1, 2

#: Commands the flow invokes, as they appear in the plugin's programs. Named
#: rather than discovered, because "every executable in the image" would drown
#: the finding in base-image tooling we never claimed to build.
FLOW_TOOLS = ("yosys", "openroad", "klayout", "iverilog", "verilator",
              "ngspice", "magic", "netgen", "sby", "eqy", "strm2gds")


def _sh(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def copied_paths(dockerfile: Path) -> Dict[str, List[str]]:
    """tool -> the image paths the root Dockerfile takes from that tool's artefact.

    Reads `COPY --from=img-<tool> <src> <dst>`; the DESTINATION is what matters,
    since that is where the composed image will hold our build.
    """
    out: Dict[str, List[str]] = {}
    for m in re.finditer(
            r"^COPY\s+--from=img-([A-Za-z0-9_-]+)\s+(\S+)\s+(\S+)",
            dockerfile.read_text(errors="replace"), re.M):
        out.setdefault(m.group(1).replace("_", "-"), []).append(m.group(3))
    return out


def check(image: str, dockerfile: Path) -> List[dict]:
    ours = copied_paths(dockerfile)
    all_dests = sorted({d for v in ours.values() for d in v})
    findings: List[dict] = []

    script = "; ".join(
        f'p=$(command -v {t} 2>/dev/null); '
        f'echo "{t} ${{p:-NONE}} $(readlink -f "$p" 2>/dev/null)"'
        for t in FLOW_TOOLS)
    rc, out, err = _sh(["docker", "run", "--rm", "--entrypoint", "bash",
                        image, "-lc", script])
    if rc != 0 and not out.strip():
        return [{"tool": "*", "problem": f"could not run {image}: "
                                        f"{err.strip()[-200:]}"}]

    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 2 or parts[0] not in FLOW_TOOLS:
            continue
        tool, path = parts[0], parts[1]
        real = parts[2] if len(parts) > 2 else path
        if path == "NONE":
            findings.append({"tool": tool, "resolved": None,
                             "problem": "not on PATH in the composed image"})
            continue
        if any(real == d or real.startswith(d.rstrip("/") + "/")
               for d in all_dests):
            continue
        # It resolves somewhere we did not copy from any artefact. If we built
        # this tool at all, the flow is running someone else's build of it.
        built = tool in ours or any(tool in k for k in ours)
        findings.append({
            "tool": tool, "resolved": real,
            "ours": sorted(ours.get(tool, [])),
            "problem": ("resolves outside every path copied from our artefact"
                        if built else
                        "we copy nothing for this tool — it is the base "
                        "image's, which may be intended"),
            "we_build_it": built,
        })
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="composed image to inspect")
    ap.add_argument("--dockerfile",
                    default=str(Path(__file__).resolve().parents[1] / "Dockerfile"))
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on a tool that is not ours")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    df = Path(a.dockerfile)
    if not df.is_file():
        print(f"[NOT CHECKED] no Dockerfile at {df} — nothing was compared, "
              f"which is not a clean result", file=sys.stderr)
        return RC_NOTHING

    findings = check(a.image, df)
    ours_missing = [f for f in findings if f.get("we_build_it")]

    print(f"fork_reaches_flow: {len(FLOW_TOOLS)} tool(s) checked in {a.image}, "
          f"{len(FLOW_TOOLS) - len(findings)} resolve into our build, "
          f"{len(ours_missing)} we build but do not run")
    for f in findings:
        print(f"  {f['tool']:<11} {str(f.get('resolved')):<44} {f['problem']}")
        for o in f.get("ours", []):
            print(f"              ours is at: {o}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "fork_reaches_flow_check", "image": a.image,
             "findings": findings}, indent=2) + "\n", encoding="utf-8")

    if ours_missing:
        print(f"[{'FAIL' if a.strict else 'REPORT'}] {len(ours_missing)} tool(s) "
              f"are built, pinned, published and versioned, and the flow runs a "
              f"different build of them", file=sys.stderr)
        return RC_NOT_OURS if a.strict else RC_OK
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
