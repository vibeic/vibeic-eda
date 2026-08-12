#!/usr/bin/env python3
"""Read provenance back out of a BUILT image and check it against the pins.

Everything else in `tools/` checks what the source files SAY. This checks what
the image actually CONTAINS, and the difference is the whole reason it exists:

  * A tag can be moved. `eda-tool-yosys:edb458a` is a mutable pointer in a
    registry — nothing stops it being repushed from a different commit, and the
    composed image would then contain binaries no pin names.
  * A `COPY` can be dropped. Deleting one line means the base image's own copy
    of that tool survives, so every command still runs, every smoke test passes,
    and the image quietly ships a tool from `hpretl/iic-osic-tools` instead of
    ours. Nothing about that failure is visible from outside.

So each tool records its repo and commit in `/vibeic/provenance/<tool>.json`
during its own build, and this asserts the composed image carries every one of
those files, each naming a `vibeic/` repo, each at the commit its pin names.

A THIRD failure, which is this check's own (vibeic-eda#117 finding 5): a tool
the map below does not name is not checked, and says so nowhere. Measured on
3187811 — the stage-8 verification toolchain (cocotb, cocotb-coverage, pyuvm,
sby) wrote no record at all, and `fastercap` wrote one the image carried and
this file never opened. Five tools, a confident PASS, nothing behind it for any
of them. `fork-gatekeeper/test_image_provenance_covers_every_tool.py` now
requires the map, the emitters and the collector stage to name one set, so
adding a tool to one of the three and not the others fails by name.

Usage:  check_image_provenance.py <image>

Exit: 0 all agree, 1 a disagreement, 2 the image could not be inspected
(which is not a pass).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Tools whose pin is written in the ROOT Dockerfile rather than in a
#: `tools/<tool>/Dockerfile`: source stages in the composing file, cloned by SHA
#: and installed or copied straight into the runtime, so they have no per-tool
#: artefact directory. The stage-8 verification toolchain comes from `tb-src`;
#: `asap7-lvs` is the ASAP7 golden CDL / hspice models / KLayout tech from
#: `asap7-lvs-src` (vibeic-eda#104). Named explicitly rather than inferred from
#: "does `tools/<tool>/Dockerfile` exist" — under that rule a mistyped tool name
#: would fall through to the root Dockerfile, find no such ARG, and compare the
#: image against the empty string.
ROOT_PINNED = {"cocotb", "cocotb-coverage", "pyuvm", "sby", "asap7-lvs"}

#: tool -> the ARG that its provenance must report, in `pin_file(tool)`.
#: Multi-source artefacts report the first; the second is checked via the tag.
PRIMARY = {
    "openroad": "OPENROAD_REF", "yosys": "YOSYS_REF",
    "sat-solvers": "KISSAT_REF", "ngspice": "NGSPICE_REF",
    "lvs": "MAGIC_REF", "iverilog": "IVERILOG_REF",
    "klayout": "KLAYOUT_REF", "verilator": "VERILATOR_REF",
    "gtkwave": "GTKWAVE_REF", "xschem": "XSCHEM_REF",
    "slang": "SLANG_REF", "xyce": "XYCE_REF",
    "yices2": "YICES2_REF", "sv-elab": "SV_ELAB_REF",
    "fastercap": "FASTERCAP_REF",
    "cocotb": "COCOTB_REF", "cocotb-coverage": "COCOTB_COVERAGE_REF",
    "pyuvm": "PYUVM_REF", "sby": "SBY_REF",
    "asap7-lvs": "ASAP7SC_REF",
}
ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)


def pin_file(tool: str) -> Path:
    """The Dockerfile the tool's `PRIMARY` ARG is written in."""
    return ROOT / "Dockerfile" if tool in ROOT_PINNED else ROOT / "tools" / tool / "Dockerfile"

# The dated exceptions are shared with check_fork_only, deliberately. If this
# check applied the rule more strictly than the one that gates the source, a
# source could pass the file check and then fail the image check for the same
# reason — and the obvious way out of that would be to weaken one of them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fork_only import pending as _pending  # noqa: E402


def run(*cmd) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def pinned_forks() -> dict:
    """Every vibeic fork the build clones at a pinned commit — the denominator.

    Read from `check_pins_current.pin_sites`, which is the same reader the
    release record uses, so this cannot drift away from what `pins_fingerprint`
    covers.
    """
    sys.path.insert(0, str(ROOT / "fork-gatekeeper"))
    from check_pins_current import pin_sites          # noqa: E402
    return {repo: site["ref"] for repo, site in pin_sites(ROOT).items()}


def covered_forks(record: dict) -> set:
    """The forks ONE provenance record makes a statement about.

    `repo` is the artefact's primary source. `also` is the optional map a
    multi-source stage uses to name the rest, so a record can cover more than one
    fork without the checker having to know which stage built what.
    """
    out = set()
    repo = record.get("repo") or ""
    m = re.search(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo, re.I)
    if m:
        out.add(m.group(1))
    also = record.get("also")
    if isinstance(also, dict):
        out |= set(also)
    return out


def main(argv) -> int:
    if len(argv) != 1:
        print(__doc__.strip().split("\n\n")[-2], file=sys.stderr)
        return 2
    image = argv[0]

    rc, out = run("docker", "image", "inspect", image)
    if rc != 0:
        print(f"check_image_provenance: cannot inspect {image} — nothing was "
              f"verified, which is a gap in the check, not a pass\n{out}",
              file=sys.stderr)
        return 2

    # `sleep infinity` with an explicit entrypoint: the image's own entrypoint
    # runs a login shell that may not exit, and a container that never starts
    # would look identical to a missing file.
    rc, cid = run("docker", "run", "-d", "--entrypoint", "sleep", image, "infinity")
    if rc != 0:
        print(f"check_image_provenance: cannot start {image}\n{cid}", file=sys.stderr)
        return 2
    cid = cid.strip()

    PENDING = _pending()
    bad, deferred, seen = [], [], 0
    covered: set = set()
    try:
        for tool, key in PRIMARY.items():
            want = ARG.findall(pin_file(tool).read_text())
            want = dict(want).get(key, "").split("#")[0].strip()
            rc, body = run("docker", "exec", cid, "cat",
                           f"/vibeic/provenance/{tool}.json")
            if rc != 0:
                bad.append(f"{tool}: no /vibeic/provenance/{tool}.json in the "
                           f"image. Either the COPY was dropped — in which case "
                           f"the base image's own {tool} is what shipped — or the "
                           f"artefact never carried one.")
                continue
            seen += 1
            try:
                got = json.loads(body)
            except Exception:
                bad.append(f"{tool}: provenance is not JSON: {body[:120]!r}")
                continue
            covered |= covered_forks(got)
            repo, ref = got.get("repo", ""), got.get("ref", "")
            if not re.match(r"https://github\.com/vibeic/", repo, re.I):
                slug = re.sub(r"^https://github\.com/", "", repo, flags=re.I)
                slug = re.sub(r"\.git$", "", slug).lower()
                exc = PENDING.get(slug)
                if exc and not exc["expired"]:
                    deferred.append(f"{tool}: built from {repo} "
                                    f"(pending fork, expires {exc.get('expires')})")
                elif exc:
                    bad.append(f"{tool}: built from {repo}, whose exception in "
                               f"PENDING_FORKS.json EXPIRED on {exc.get('expires')}")
                else:
                    bad.append(f"{tool}: built from {repo!r}, which is not ours")
            if ref != want:
                bad.append(f"{tool}: the image contains a different commit than "
                           f"the pin names\n      image {ref}\n      pin   {want}")
    finally:
        run("docker", "rm", "-f", cid)

    if seen == 0:
        print("check_image_provenance: found no provenance file at all — this "
              "image predates the per-tool split, or every COPY was dropped. "
              "Either way nothing was verified.", file=sys.stderr)
        return 2

    if bad:
        print("check_image_provenance: %d disagreement(s) between %s and its "
              "pins\n" % (len(bad), image), file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        return 1

    if deferred:
        print("check_image_provenance: %d tool(s) built from a source under a "
              "DATED exception:" % len(deferred))
        for d in deferred:
            print("  " + d)
    print("check_image_provenance: %d/%d tools in %s are at the pinned commit, "
          "%d from a vibeic/ repo" % (seen, len(PRIMARY), image, seen - len(deferred)))

    # AND THE DENOMINATOR, STATED (vibeic-eda#104 finding 2). `19/19` was true
    # and read as "every fork we ship". It is 19 of the records that exist, and
    # a fork with no record is not a FAIL here — it is absent from both sides of
    # the fraction, which is why asap7sc7p5t_28 could be unpinned, unrecorded and
    # unmentioned while this printed a confident PASS over it. The set that
    # matters is the forks the build CLONES, so that is what is divided into, and
    # the ones no record speaks for are NAMED rather than silently excluded.
    #
    # Not an error: a missing record is a gap in what the image records, and
    # failing here would only invite the record to be written without being
    # checked. It is printed on every run so it cannot go unnoticed, and the
    # remedy is a record from the stage that clones it, exactly as `asap7-lvs`
    # now does.
    try:
        pins = pinned_forks()
    except Exception as exc:                      # never turn a PASS into a crash
        print(f"  (could not compute the fork denominator: {exc})")
        return 0
    unspoken = sorted(set(pins) - covered)
    print("check_image_provenance: %d of %d pinned fork(s) are named by a record "
          "in the image" % (len(pins) - len(unspoken), len(pins)))
    if unspoken:
        print("  NOT CHECKED — no provenance record in the image speaks for "
              "these, so nothing here verifies what commit they carry:")
        for repo in unspoken:
            print(f"    {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
