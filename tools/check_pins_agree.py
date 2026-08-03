#!/usr/bin/env python3
"""A tool's commit is stated in three places. They must say the same thing.

The per-tool split buys build isolation at the cost of writing each pin down
more than once:

  1. ``tools/<name>/Dockerfile``  ``ARG YOSYS_REF=<sha>``   — what gets compiled
  2. ``docker-bake.hcl``          ``variable "YOSYS_REF"``  — what gets tagged
  3. ``Dockerfile``               ``ARG IMG_YOSYS=...:<short>`` — what gets pulled

Drift between them does not fail the build; it produces a release that quietly
contains something other than what the pin says. Bump (1) alone and the tag in
(2) never moves, so the registry keeps serving the image built before the bump
and the "new version" ships the old binaries. Bump (1) and (2) but not (3) and
the composed image pulls a tag nobody built — that one at least fails loudly,
which makes it the *least* dangerous of the three.

So this asserts the loop closes: every tool named in the bake file has a
Dockerfile stating the same full SHA, and the composing Dockerfile pulls a tag
derived from exactly those SHAs.

Multi-source artefacts (``sat-solvers`` = kissat + cadical, ``lvs`` = magic +
netgen) are tagged ``<short1>-<short2>`` and both halves are checked, because a
tag keyed on only one of them cannot move when the other does.

Exit: 0 agree, 1 drift found, 2 nothing was compared (not a pass).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: tool -> the ARG names whose SHAs form its tag, in tag order.
TOOLS = {
    "openroad":    ["OPENROAD_REF"],
    "yosys":       ["YOSYS_REF"],
    "sat-solvers": ["KISSAT_REF", "CADICAL_REF"],
    "ngspice":     ["NGSPICE_REF"],
    "lvs":         ["MAGIC_REF", "NETGEN_REF"],
    "iverilog":    ["IVERILOG_REF"],
    "klayout":     ["KLAYOUT_REF"],
    "verilator":   ["VERILATOR_REF"],
    "gtkwave":     ["GTKWAVE_REF"],
    "xschem":      ["XSCHEM_REF"],
    "slang":       ["SLANG_REF"],
    "xyce":        ["XYCE_REF"],
    # vibeic-eda#60 — three forks the image used to inherit from the base
    # image. Registered here at the same time as the bake target, because
    # this gate's own message is that an unregistered tool is one whose
    # pins are never compared while the summary counts agreement.
    "sv2v":        ["SV2V_REF"],
    "ciel":        ["CIEL_REF"],
    "ihp-open-pdk": ["IHP_OPEN_PDK_REF"],
    "yices2":      ["YICES2_REF"],
    "sv-elab":     ["SV_ELAB_REF"],
    "fault":       ["FAULT_REF"],
    # Three sibling repos, because upstream splits FasterCap across three and
    # its CMakeLists does `add_subdirectory("../LinAlgebra")`. All three refs
    # are in the tag, so none can move without the release pulling a new image.
    "fastercap":   ["FASTERCAP_REF", "LINALGEBRA_REF", "GEOMETRY_REF"],
}

ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)
#: Both `*_REF` (the source commit) and `*_RECIPE` (the digest of HOW it is
#: built). The pattern used to match only `_REF`, so the recipe half of every
#: tag was invisible to this check on the bake side.
BAKE_VAR = re.compile(
    r'^variable\s+"([A-Z0-9_]+(?:_REF|_RECIPE))"\s*\{\s*default\s*=\s*"([^"]+)"',
    re.M)


def args_of(path: Path) -> dict:
    if not path.is_file():
        return {}
    return {m.group(1): m.group(2).strip("\"'").split("#")[0].strip()
            for m in ARG.finditer(path.read_text())}


def main() -> int:
    bake_p, compose_p = ROOT / "docker-bake.hcl", ROOT / "Dockerfile"
    if not bake_p.is_file() or not compose_p.is_file():
        print("check_pins_agree: docker-bake.hcl or Dockerfile missing — nothing "
              "was compared, which is a gap in the check, not a pass",
              file=sys.stderr)
        return 2

    bake_text = bake_p.read_text()
    bake = dict(BAKE_VAR.findall(bake_text))
    compose = args_of(compose_p)
    bad, compared = [], 0

    # EVERY BUILT TOOL MUST BE IN `TOOLS`, or its pins are simply never
    # compared and this check reports agreement it never looked for.
    #
    # The table above is hand-written, so a tool added to docker-bake.hcl and
    # not to it gets ZERO pin verification while the summary line still says
    # "N pin(s) across M tool(s) agree". Measured on main when this was added:
    # `fault` had been built and unregistered — its pins happened to be
    # correct, which is the only reason it was latent rather than a second #41.
    #
    # Derived from what is BUILT (a bake target that also has a
    # tools/<name>/Dockerfile), not from the tools directory alone: that
    # directory can hold scaffolding the bake file does not build, and failing
    # on those would be a finding about nothing.
    built = ({m.group(1) for m in re.finditer(r'^target\s+"([\w-]+)"',
                                              bake_text, re.M)}
             & {d.parent.name for d in (ROOT / "tools").glob("*/Dockerfile")})
    for name in sorted(built - set(TOOLS)):
        bad.append(f"{name}: docker-bake.hcl builds it and tools/{name}/"
                   f"Dockerfile pins it, but TOOLS in this file does not list "
                   f"it — so none of its pins are compared and the summary "
                   f"below counts agreement it never looked for")

    for tool, keys in TOOLS.items():
        tf = ROOT / "tools" / tool / "Dockerfile"
        if not tf.is_file():
            bad.append(f"{tool}: docker-bake.hcl builds it but tools/{tool}/"
                       f"Dockerfile does not exist")
            continue
        tool_args = args_of(tf)

        for k in keys:
            compared += 1
            in_tool, in_bake = tool_args.get(k), bake.get(k)
            if in_tool is None:
                bad.append(f"{tool}: tools/{tool}/Dockerfile states no {k}, so "
                           f"the bake variable pins nothing")
            elif in_bake is None:
                bad.append(f"{tool}: docker-bake.hcl has no variable {k}, so a "
                           f"bump in tools/{tool}/Dockerfile cannot move the tag "
                           f"and the release would keep pulling the old image")
            elif in_tool != in_bake:
                bad.append(f"{tool}: {k} disagrees\n"
                           f"      tools/{tool}/Dockerfile  {in_tool}\n"
                           f"      docker-bake.hcl          {in_bake}")

        # The composing Dockerfile must pull the tag those SHAs produce.
        var = "IMG_" + tool.upper().replace("-", "_")
        compared += 1
        pulled = compose.get(var)
        if pulled is None:
            bad.append(f"{tool}: Dockerfile has no {var}, so the composed image "
                       f"never copies this tool in")
            continue
        # The tag carries a RECIPE component too — a digest of the tool's own
        # Dockerfile — so that changing HOW a tool is built moves the tag even
        # when the source commit does not. This check did not know about it, so
        # it reported all eight tools as disagreeing while nothing was wrong:
        # the loudest possible false alarm, and the shape that gets a check
        # deleted rather than fixed.
        want_tag = "-".join((tool_args.get(k) or "?")[:7] for k in keys)
        rf = ROOT / "tools" / tool / "Dockerfile"
        if rf.is_file():
            import hashlib
            recipe = hashlib.sha256(rf.read_bytes()).hexdigest()[:6]
            want_tag += "-" + recipe
            # THE RECIPE HAS THREE SITES AND THIS CHECKED TWO. The digest is
            # OF tools/<t>/Dockerfile (1); docker-bake.hcl's <T>_RECIPE decides
            # what the built image is TAGGED (2); the root Dockerfile decides
            # what is PULLED (3). Comparing only (1) against (3) leaves (2)
            # free to drift, and (2) is the mirror of vibe-ic-eda#41: `bake`
            # publishes under one tag while the composing Dockerfile pulls
            # another, so the pull fails on an image nobody built. Measured
            # when this was added: setting FAULT_RECIPE to a wrong value left
            # this check printing "32 pin(s) across 15 tool(s) agree", rc 0.
            declared = bake.get(tool.upper().replace("-", "_") + "_RECIPE")
            compared += 1
            if declared is None:
                bad.append(f"{tool}: docker-bake.hcl has no "
                           f"{tool.upper().replace('-', '_')}_RECIPE, so the "
                           f"built image is tagged by something this check "
                           f"cannot see")
            elif declared != recipe:
                bad.append(
                    f"{tool}: the RECIPE digest disagrees — `bake` would "
                    f"publish a tag the composing Dockerfile does not pull\n"
                    f"      docker-bake.hcl          {declared}\n"
                    f"      sha256(tools/{tool}/Dockerfile)[:6]  {recipe}")
        want = f"ghcr.io/vibeic/eda-tool-{tool}:{want_tag}"
        if pulled != want:
            bad.append(f"{tool}: Dockerfile pulls a tag the pins do not produce\n"
                       f"      pulls  {pulled}\n"
                       f"      pins   {want}")

    if compared == 0:
        print("check_pins_agree: compared 0 pins — the patterns no longer match "
              "these files, so nothing was checked", file=sys.stderr)
        return 2

    if bad:
        print("check_pins_agree: %d disagreement(s) across the three places a "
              "pin is written\n" % len(bad), file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        print("\n  A pin that disagrees with itself does not fail the build — it\n"
              "  ships a release containing something other than what it says.",
              file=sys.stderr)
        return 1

    print("check_pins_agree: %d pin(s) across %d tool(s) agree in all three "
          "places" % (compared, len(TOOLS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
