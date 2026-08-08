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

#: Targets whose tag is composed from more than one binary, so the command name
#: cannot be derived from the target name.
#:
#: `yices2` was missing and cost this program its credibility for weeks: the
#: target ships `yices`, `yices-sat`, `yices-smt` and `yices-smt2`, and NOTHING
#: is named `yices2`. So every run reported "yices2: not on PATH in the composed
#: image" about a tool that is present, ours, and symlinked four ways — measured:
#: all four resolve to /foss/tools/yices/bin, copied from `img-yices2`, and the
#: in-image provenance names vibeic/yices2 at the pinned commit.
_MULTI_BINARY = {"lvs": ("magic", "netgen"),
                 "sat-solvers": ("kissat", "cadical"),
                 "yices2": ("yices", "yices-sat", "yices-smt", "yices-smt2")}

#: Targets that deliberately install NO command. Reporting these as "not on
#: PATH" is true and useless — it is the intended state, recorded elsewhere with
#: its reason, and a finding that never goes away is a finding nobody reads.
#:
#: sv-elab IS the slang yosys plugin. yosys 0.67+ carries the slang frontend
#: itself, so installing ours puts a second copy in the process and the
#: duplicated statics double-free at exit (vibeic-eda#24). The fork stays tracked
#: and its artefact stays built; only the install is declined.
_NO_COMMAND = {"sv-elab": "vibeic-eda#24 — a second slang frontend in one "
                          "process double-frees at exit; the artefact is built "
                          "and pinned, the install is declined",
               "ihp-open-pdk": "a PDK data directory (COPY'd straight to "
                          "/foss/pdks/ihp-sg13g2), not a binary — probing it "
                          "with `command -v ihp-open-pdk` always prints NONE "
                          "regardless of whether the PDK is correctly staged, "
                          "the same category error `command -v ihp-sg13g2` "
                          "would be. Its actual presence is verified "
                          "elsewhere: the ngspice OSDI compile step "
                          "(Dockerfile ~L542-560) depends on and would fail "
                          "without /foss/pdks/ihp-sg13g2 existing, and "
                          "check_image_claims covers its version provenance."}

#: Commands installed from OUR source tree rather than copied from an artefact,
#: so path containment cannot see them. The claim this program can make about
#: them is weaker and is stated as such: the source tree we build from is
#: present in the image at the named path.
#:
#: sby is `make -C /opt/vibeic-forks/sby install PREFIX=/usr/local`, which lands
#: in /usr/local/bin — a path no `COPY --from=img-*` mentions. The check reported
#: it as "the base image's, which may be intended". It is not the base's:
#: measured, `sby --version` reports v0.67-26-g7422136 against the pin
#: 742213689, i.e. our commit.
_INSTALLED_FROM_SOURCE = {"sby": "/opt/vibeic-forks/sby"}

#: Commands that are not tool targets but belong in the sweep: shipped by the
#: root Dockerfile directly, or buddy tools of a target.
_EXTRA = ("sby", "eqy", "strm2gds")


def flow_tools(dockerfile: Path) -> tuple:
    """Every command we should be able to claim, DERIVED rather than listed.

    The first version was a hand-written tuple, and vibeic-eda#23 caught what
    that costs: gtkwave, slang, xschem and Xyce were shipping the base image's
    builds — the exact defect this file exists to find — and none of the four
    was in the list. A check that only knows the tools you already suspected
    finds the tools you already suspected.

    So the set comes from what the composing image actually takes from our own
    artefacts (`COPY --from=img-<target>`), which is the same source the
    containment test uses. A tool added tomorrow is covered tomorrow, with no
    edit here.

    `_MULTI_BINARY` and `_EXTRA` remain hand-held because they cannot be
    derived: `lvs` ships two differently-named binaries, and `sby`/`eqy` are
    installed by the root Dockerfile rather than as targets.
    """
    names: list = []
    for target in copied_paths(dockerfile):
        names.extend(_MULTI_BINARY.get(target, (target,)))
    names.extend(_EXTRA)
    return tuple(dict.fromkeys(names))


def _sh(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def copied_paths(dockerfile: Path) -> Dict[str, List[str]]:
    """tool -> the image paths the root Dockerfile takes from that tool's artefact.

    Reads `COPY --from=img-<tool> <src>... <dst>`; the DESTINATION is what
    matters, since that is where the composed image will hold our build.

    A `COPY` line can name MORE THAN ONE source before the final destination
    — Dockerfile:413 (`COPY --from=img-openroad .../openroad .../sta
    .../bin/`) and :487 (`COPY --from=img-sat-solvers .../kissat .../cadical
    .../bin/`) both do. The prior version fixed the capture at exactly two
    `(\\S+)` groups after the tool name, so on a 3-source-plus-destination
    line it grabbed the SECOND SOURCE as if it were the destination and
    never saw the real one — openroad's own containment check then compared
    the resolved binary's path against `[.../sta]` and reported it as
    "resolves outside every path copied from our artefact" (a false
    positive: the binary WAS ours, the check was looking at the wrong
    token). `kissat`/`cadical` hit the identical bug but happened to print
    text that run_tick.sh's grep filter does not match, so it never
    surfaced there — same defect, silent in one caller and loud in another.

    Fixed by taking the LAST whitespace-separated token on the line as the
    destination, whatever the source count: `docker build` itself requires
    the final COPY argument to be the destination when there is more than
    one source, so this is not a heuristic — it is the same rule the builder
    enforces.
    """
    out: Dict[str, List[str]] = {}
    for m in re.finditer(
            r"^COPY[ \t]+--from=img-([A-Za-z0-9_-]+)[ \t]+(\S+(?:[ \t]+\S+)*)$",
            dockerfile.read_text(errors="replace"), re.M):
        tokens = m.group(2).split()
        dst = tokens[-1]
        out.setdefault(m.group(1).replace("_", "-"), []).append(dst)
    return out


def provenance_drift(image: str, pins: Dict[str, str]) -> List[dict]:
    """Tools whose in-image provenance names a DIFFERENT commit than the pin.

    Path containment proves ORIGIN; this proves CURRENCY. The two are separate
    failures and only one of them is visible from where the binary sits: an image
    can hold our build of the right tool from the wrong commit, and every path
    check passes.

    The provenance file is written INSIDE each tool artefact, so it travels with
    the bytes — a tag can be moved, a file in the image cannot.

    A worked non-finding, recorded because the reasoning is the point: 0.2.31's
    `sby --version` reports `v0.67-21-g3729822` against a pin of `742213689`,
    five commits ahead. That is not drift — `SBY_REF` moved at 10:11 and the
    image was built at 04:21, so the image simply predates the pin. Comparing
    today's source against an older binary is how a stale image gets reported as
    a defect, and this function is only meaningful run against a FRESH image.
    """
    rc, out, _ = _sh(["docker", "run", "--rm", "--entrypoint", "bash", image,
                      "-lc", "cat /vibeic/provenance/*.json 2>/dev/null"])
    if rc != 0 or not out.strip():
        # NOT CHECKED is its own state. Returning this as a drift row made
        # 0.2.31 — which predates provenance entirely — report "1 tool built
        # from a commit the pins no longer name", which is a finding about
        # nothing. A probe that found nothing must not look like a pass OR like
        # a finding.
        return [{"tool": "*", "not_checked": True,
                 "problem": "the image carries no provenance files — nothing "
                            "was compared, which is not a clean result"}]
    drift: List[dict] = []
    for ln in out.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        tool, ref = d.get("tool", ""), d.get("ref", "")
        repo = (d.get("repo") or "").rstrip("/").removesuffix(".git").split("/")[-1]
        pin = pins.get(repo) or pins.get(tool)
        if not pin or not ref:
            continue
        if ref != pin:
            drift.append({"tool": tool, "built_from": ref[:9], "pin": pin[:9],
                          "problem": "the image was built from a different "
                                     "commit than the pin now names"})
    return drift


def check(image: str, dockerfile: Path) -> List[dict]:
    ours = copied_paths(dockerfile)
    all_dests = sorted({d for v in ours.values() for d in v})
    findings: List[dict] = []

    FLOW_TOOLS = flow_tools(dockerfile)
    script = "; ".join(
        f'p=$(command -v {t} 2>/dev/null); '
        f'echo "{t} ${{p:-NONE}} $(readlink -f "$p" 2>/dev/null)"'
        for t in FLOW_TOOLS)
    rc, out, err = _sh(["docker", "run", "--rm", "--entrypoint", "bash",
                        image, "-lc", script])
    if rc != 0 and not out.strip():
        return [{"tool": "*", "problem": f"could not run {image}: "
                                        f"{err.strip()[-200:]}"}]

    # Which source trees of ours are present, for the tools installed by `make
    # install` rather than copied. Asked once, in the image, not assumed.
    src_present = {}
    if _INSTALLED_FROM_SOURCE:
        probe = "; ".join(f'[ -d {p} ] && echo {t}'
                          for t, p in _INSTALLED_FROM_SOURCE.items())
        _, sout, _ = _sh(["docker", "run", "--rm", "--entrypoint", "bash",
                          image, "-lc", probe])
        src_present = {ln.strip() for ln in sout.splitlines() if ln.strip()}

    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 2 or parts[0] not in FLOW_TOOLS:
            continue
        tool, path = parts[0], parts[1]
        real = parts[2] if len(parts) > 2 else path
        if path == "NONE":
            if tool in _NO_COMMAND:
                # Intended, and recorded with its reason. A permanent finding is
                # one nobody reads, and it drowns the findings that matter.
                continue
            findings.append({"tool": tool, "resolved": None,
                             "problem": "not on PATH in the composed image"})
            continue
        if any(real == d or real.startswith(d.rstrip("/") + "/")
               for d in all_dests):
            continue
        if tool in _INSTALLED_FROM_SOURCE:
            src = _INSTALLED_FROM_SOURCE[tool]
            if tool in src_present:
                continue
            findings.append({
                "tool": tool, "resolved": real,
                "problem": f"installed from our source tree, but {src} is not "
                           f"in the image — so what is on PATH did not come "
                           f"from us",
                "we_build_it": True})
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_pins_current import pinned_refs               # noqa: E402
    drift = provenance_drift(a.image, pinned_refs(df.parent))
    ours_missing = [f for f in findings if f.get("we_build_it")]

    tools_checked = len(flow_tools(df))
    print(f"fork_reaches_flow: {tools_checked} tool(s) checked in {a.image}, "
          f"{tools_checked - len(findings)} resolve into our build, "
          f"{len(ours_missing)} we build but do not run")
    for f in findings:
        print(f"  {f['tool']:<11} {str(f.get('resolved')):<44} {f['problem']}")
        for o in f.get("ours", []):
            print(f"              ours is at: {o}")

    for d in drift:
        print(f"  {d['tool']:<11} {d.get('built_from','?'):<10} vs pin "
              f"{d.get('pin','?'):<10} {d['problem']}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "fork_reaches_flow_check", "image": a.image,
             "findings": findings, "provenance_drift": drift}, indent=2) + "\n",
            encoding="utf-8")

    unchecked = [d for d in drift if d.get("not_checked")]
    real_drift = [d for d in drift if not d.get("not_checked")]
    if unchecked:
        print(f"[NOT CHECKED] provenance: {unchecked[0]['problem']}",
              file=sys.stderr)
    if real_drift:
        print(f"[{'FAIL' if a.strict else 'REPORT'}] {len(real_drift)} tool(s) "
              f"in the image were built from a commit the pins no longer name",
              file=sys.stderr)

    if ours_missing:
        print(f"[{'FAIL' if a.strict else 'REPORT'}] {len(ours_missing)} tool(s) "
              f"are built, pinned, published and versioned, and the flow runs a "
              f"different build of them", file=sys.stderr)
        return RC_NOT_OURS if a.strict else RC_OK
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
