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
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from capability_smoke import run_container_probe

RC_OK, RC_NOT_OURS, RC_NOTHING = 0, 1, 2
PROBE_STALL_GRACE_S = float(
    os.environ.get("GK_FORK_REACHES_FLOW_STALL_GRACE", "300")
)

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
                 "klayout": ("klayout", "strm2gds"),
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
               "ihp-open-pdk": "PDK data directories (COPY'd straight to "
                          "/foss/pdks/, both IHP families), not a binary — probing it "
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


@dataclass(frozen=True)
class ProbeRun:
    """Docker completion plus whether a complete measurement was possible."""

    rc: int
    out: str
    err: str
    outcome: str = "natural"

    def __iter__(self):
        yield self.rc
        yield self.out
        yield self.err

    @property
    def measurable(self) -> bool:
        return self.outcome == "natural"


def _as_run(value) -> ProbeRun:
    """Normalise the historical tuple returned by test fixtures."""
    if isinstance(value, ProbeRun):
        return value
    if hasattr(value, "outcome") and hasattr(value, "rc"):
        return ProbeRun(int(value.rc), getattr(value, "output", ""),
                        getattr(value, "err", ""), str(value.outcome))
    rc, out, err = value
    return ProbeRun(int(rc), out or "", err or "", "natural")


def _sh(cmd, timeout=None):
    """Run a Docker probe under a renewable activity lease, with no cap."""
    del timeout  # compatibility only; elapsed time is not a verdict
    try:
        bash_i = cmd.index("bash")
        image, script = cmd[bash_i + 1], cmd[-1]
    except (ValueError, IndexError):
        return ProbeRun(127, "", "unsupported Docker probe command",
                        "launch_error")
    try:
        got = run_container_probe(image, script, PROBE_STALL_GRACE_S)
    except Exception as exc:                                   # noqa: BLE001
        return ProbeRun(127, "", f"{type(exc).__name__}: {exc}",
                        "launch_error")
    return ProbeRun(got.rc, got.output, "", got.outcome)


def _not_checked(what: str, run: ProbeRun, *, tool: str = "*") -> dict:
    detail = (run.err or run.out).strip().splitlines()
    suffix = f": {detail[-1][:200]}" if detail else ""
    state = "stalled" if run.outcome == "stalled" else "unmeasurable"
    return {
        "tool": tool,
        "not_checked": True,
        "probe_outcome": run.outcome,
        "probe_rc": run.rc,
        "problem": f"{what} was {state}{suffix}",
    }


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
    text = dockerfile.read_text(errors="replace")
    for m in re.finditer(
            r"^COPY[ \t]+--from=img-([A-Za-z0-9_-]+)[ \t]+(\S+(?:[ \t]+\S+)*)$",
            text, re.M):
        tokens = m.group(2).split()
        dst = tokens[-1]
        out.setdefault(m.group(1).replace("_", "-"), []).append(dst)

    # The composition also installs artefacts with a read-only BuildKit bind
    # mount followed by `cp -a`. A mount by itself installs nothing. Recognise
    # only the leading, unconditional literal copy; do not infer shell branches,
    # variables, later commands, or a copy from outside that mount.
    logical = re.sub(r"\\\r?\n", " ", text)
    literal = r"/[A-Za-z0-9_./+-]+"
    for m in re.finditer(
            r"^RUN[ \t]+((?:--mount=\S+[ \t]+)+)"
            rf"cp[ \t]+-a[ \t]+({literal})[ \t]+({literal})"
            r"[ \t]*(?=&&|$)", logical, re.M):
        src, dst = m.group(2), m.group(3)
        if ".." in Path(src).parts or ".." in Path(dst).parts:
            continue
        for mount in m.group(1).split():
            opts = dict(part.split("=", 1) if "=" in part else (part, "")
                        for part in mount.removeprefix("--mount=").split(","))
            target, stage = opts.get("target", ""), opts.get("from", "")
            if (opts.get("type") == "bind" and re.fullmatch(r"img-[A-Za-z0-9_-]+", stage)
                    and ("ro" in opts or "readonly" in opts)
                    and target.startswith("/")
                    and ".." not in Path(target).parts
                    and (src == target or src.startswith(target.rstrip("/") + "/"))):
                out.setdefault(stage[4:].replace("_", "-"), []).append(dst)
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
    run = _as_run(_sh([
        "docker", "run", "--rm", "--entrypoint", "bash", image, "-lc",
        "set -- /vibeic/provenance/*.json; "
        "if [ ! -e \"$1\" ]; then "
        "printf '%s\\n' __VIBEIC_NO_PROVENANCE__; exit 0; fi; "
        "cat \"$@\"",
    ]))
    if not run.measurable or run.rc != 0:
        return [_not_checked("reading in-image provenance", run)]
    if (not run.out.strip()
            or run.out.strip() == "__VIBEIC_NO_PROVENANCE__"):
        # NOT CHECKED is its own state. Returning this as a drift row made
        # 0.2.31 — which predates provenance entirely — report "1 tool built
        # from a commit the pins no longer name", which is a finding about
        # nothing. A probe that found nothing must not look like a pass OR like
        # a finding.
        return [{"tool": "*", "not_checked": True,
                 "probe_outcome": run.outcome, "probe_rc": run.rc,
                 "problem": "the image carries no provenance files — nothing "
                            "was compared, which is not a clean result"}]
    drift: List[dict] = []
    malformed = 0
    for ln in run.out.splitlines():
        if not ln.strip() or ln.startswith("[INFO]"):
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            malformed += 1
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
    if malformed:
        drift.append({
            "tool": "*", "not_checked": True,
            "probe_outcome": run.outcome, "probe_rc": run.rc,
            "problem": f"{malformed} provenance record(s) were malformed; "
                       "the comparison is incomplete",
        })
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
    paths = _as_run(_sh(["docker", "run", "--rm", "--entrypoint", "bash",
                         image, "-lc", script]))
    if not paths.measurable or paths.rc != 0:
        return [_not_checked(f"resolving flow tools in {image}", paths)]

    # Which source trees of ours are present, for the tools installed by `make
    # install` rather than copied. Asked once, in the image, not assumed.
    src_present = set()
    src_absent = set()
    if _INSTALLED_FROM_SOURCE:
        probe = "; ".join(
            f'if [ -d {p} ]; then echo "{t} PRESENT"; '
            f'else echo "{t} ABSENT"; fi'
            for t, p in _INSTALLED_FROM_SOURCE.items())
        sources = _as_run(_sh([
            "docker", "run", "--rm", "--entrypoint", "bash", image,
            "-lc", probe,
        ]))
        if not sources.measurable or sources.rc != 0:
            findings.extend(
                _not_checked("checking source-installed tool provenance",
                             sources, tool=tool)
                for tool in sorted(_INSTALLED_FROM_SOURCE)
            )
        else:
            for ln in sources.out.splitlines():
                parts = ln.strip().split()
                # The one-token form preserves old fixture compatibility.
                if len(parts) == 1 and parts[0] in _INSTALLED_FROM_SOURCE:
                    src_present.add(parts[0])
                elif len(parts) == 2 and parts[0] in _INSTALLED_FROM_SOURCE:
                    if parts[1] == "PRESENT":
                        src_present.add(parts[0])
                    elif parts[1] == "ABSENT":
                        src_absent.add(parts[0])
            answered = src_present | src_absent
            for tool in sorted(set(_INSTALLED_FROM_SOURCE) - answered):
                findings.append({
                    "tool": tool, "not_checked": True,
                    "probe_outcome": sources.outcome,
                    "probe_rc": sources.rc,
                    "problem": "source-tree presence probe returned no "
                               "completion record for this tool",
                })

    answered_tools = set()
    for ln in paths.out.splitlines():
        parts = ln.split()
        if len(parts) < 2 or parts[0] not in FLOW_TOOLS:
            continue
        tool, path = parts[0], parts[1]
        answered_tools.add(tool)
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
            if tool not in src_absent:
                # A retained NOT CHECKED row above already explains why this
                # source-tree fact is unavailable.  Do not fabricate a
                # measured absence from that missing answer.
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
        tool_dests = [d for target, dests in ours.items()
                      if tool in _MULTI_BINARY.get(target, (target,))
                      for d in dests]
        built = bool(tool_dests)
        findings.append({
            "tool": tool, "resolved": real,
            "ours": sorted(tool_dests),
            "problem": ("resolves outside every path copied from our artefact"
                        if built else
                        "we copy nothing for this tool — it is the base "
                        "image's, which may be intended"),
            "we_build_it": built,
        })
    for tool in sorted(set(FLOW_TOOLS) - answered_tools):
        findings.append({
            "tool": tool, "not_checked": True,
            "probe_outcome": paths.outcome, "probe_rc": paths.rc,
            "problem": "PATH-resolution probe returned no completion record "
                       "for this tool",
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
    unchecked_findings = [f for f in findings if f.get("not_checked")]
    ours_missing = [f for f in findings
                    if f.get("we_build_it") and not f.get("not_checked")]

    tools_checked = len(flow_tools(df))
    unknown_tools = {f.get("tool") for f in unchecked_findings}
    measured_tools = (0 if "*" in unknown_tools else
                      max(0, tools_checked - len(unknown_tools)))
    finding_tools = {f.get("tool") for f in findings
                     if not f.get("not_checked")}
    resolved_ours = max(0, measured_tools - len(finding_tools))
    print(f"fork_reaches_flow: {measured_tools}/{tools_checked} tool(s) "
          f"measured in {a.image}, "
          f"{resolved_ours} resolve into our build, "
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
    if unchecked_findings:
        print(f"[NOT CHECKED] {len(unchecked_findings)} flow-origin probe(s) "
              f"did not produce a complete measurement", file=sys.stderr)
    if real_drift:
        print(f"[{'FAIL' if a.strict else 'REPORT'}] {len(real_drift)} tool(s) "
              f"in the image were built from a commit the pins no longer name",
              file=sys.stderr)

    if ours_missing:
        print(f"[{'FAIL' if a.strict else 'REPORT'}] {len(ours_missing)} tool(s) "
              f"are built, pinned, published and versioned, and the flow runs a "
              f"different build of them", file=sys.stderr)
    if a.strict and (ours_missing or real_drift):
        return RC_NOT_OURS
    if unchecked or unchecked_findings:
        return RC_NOTHING
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
