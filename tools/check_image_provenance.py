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

This command fails on disagreements, missing records and unverified pinned
sources. Its existing callers retain their policy: run_tick logs it as an
advisory (|| true), while the GitHub release workflow requires a successful
exit before publishing. Watch-only repositories and prebuilt volume contents
assertions are outside the cloned-source pin inventory.

Exit: 0 all pinned sources agree, 1 a disagreement or coverage gap, 2 the
image or pin inventory could not be inspected (which is not a pass).
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
#: `asap7-lvs-src` (vibeic-eda#104). `librelane` is the vibeic/librelane commit
#: whose diffs and files the runtime stage installs over the base image's
#: LibreLane; its record is written by `librelane-src`. Named explicitly rather than inferred from
#: "does `tools/<tool>/Dockerfile` exist" — under that rule a mistyped tool name
#: would fall through to the root Dockerfile, find no such ARG, and compare the
#: image against the empty string.
ROOT_PINNED = {"cocotb", "cocotb-coverage", "pyuvm", "sby", "asap7-lvs", "librelane", "cace", "spicebind", "eqy", "align", "openroad-flow-scripts"}

#: tool -> the ARG that its provenance must report, in `pin_file(tool)`.
#: Multi-source artefacts also report dependencies, all checked against pins.
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
    "asap7-lvs": "ASAP7SC_REF", "librelane": "LIBRELANE_REF",
    "cace": "CACE_REF", "spicebind": "SPICEBIND_REF",
    "eqy": "EQY_REF",
    "fault": "FAULT_REF", "ciel": "CIEL_REF", "sv2v": "SV2V_REF",
    "ihp-open-pdk": "IHP_OPEN_PDK_REF", "align": "ALIGN_PUBLIC_REF",
    "openroad-flow-scripts": "OPENROAD_FLOW_SCRIPTS_REF",
}
ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)
FORK_REPO = re.compile(r"https://github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.I)
SHA = re.compile(r"[0-9a-f]{40}$")


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


def record_sources(record: dict) -> list[tuple[str, str, str]]:
    """Read both existing dependency encodings; neither may certify by name only."""
    if not isinstance(record, dict):
        raise ValueError("provenance must be a JSON object")
    sources = [("primary", record.get("repo", ""), record.get("ref", ""))]
    prefixes = {key.rsplit("_", 1)[0] for key in record if key.endswith(("_repo", "_ref"))}
    for prefix in sorted(prefixes):
        # Missing either half must remain visible to validation.
        sources.append((prefix, record.get(prefix + "_repo", ""),
                        record.get(prefix + "_ref", "")))
    also = record.get("also", {})
    if not isinstance(also, dict):
        raise ValueError("also must map repository names to commits")
    for repo, ref in also.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"invalid also repository name: {repo!r}")
        sources.append((repo, f"https://github.com/vibeic/{repo}.git", ref))
    return sources


def covered_forks(record: dict) -> set:
    """Names stated by a record; main validates every corresponding ref."""
    return {match.group(1) for _, repo, _ in record_sources(record)
            if isinstance(repo, str) and (match := FORK_REPO.fullmatch(repo))}


def main(argv) -> int:
    if len(argv) != 1:
        print(__doc__.strip().split("\n\n")[-2], file=sys.stderr)
        return 2
    image = argv[0]

    try:
        pins = pinned_forks()
        if not pins or any(not isinstance(ref, str) or not SHA.fullmatch(ref)
                           for ref in pins.values()):
            raise ValueError("empty or invalid cloned-source pin inventory")
    except Exception as exc:
        print(f"check_image_provenance: could not compute the fork denominator: {exc}",
              file=sys.stderr)
        return 2
    # GitHub repository identity is case-insensitive; retain canonical pin names
    # in diagnostics rather than losing OpenROAD/FasterCap-style capitalization.
    canonical = {repo.lower(): repo for repo in pins}

    rc, out = run("docker", "image", "inspect", image)
    if rc != 0:
        print(f"check_image_provenance: cannot inspect {image} — nothing was "
              f"verified, which is a gap in the check, not a pass\n{out}",
              file=sys.stderr)
        return 2

    # `sleep infinity` with an explicit entrypoint: the image's own entrypoint
    # runs a login shell that may not exit, and a container that never starts
    # would look identical to a missing file.
    rc, cid = run("docker", "run", "-d", "--read-only", "--network", "none",
                  "--entrypoint", "sleep", image, "infinity")
    if rc != 0:
        print(f"check_image_provenance: cannot start {image}\n{cid}", file=sys.stderr)
        return 2
    cid = cid.strip()

    PENDING = _pending()
    bad, deferred, seen = [], [], 0
    covered: set = set()
    try:
        for tool, key in PRIMARY.items():
            args = dict(ARG.findall(pin_file(tool).read_text()))
            want = args.get(key, "").split("#")[0].strip()
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
                sources = record_sources(got)
            except (ValueError, TypeError) as exc:
                bad.append(f"{tool}: invalid provenance: {exc}: {body[:120]!r}")
                continue
            if got.get("ref") != want:
                bad.append(f"{tool}: the image contains a different commit than "
                           f"the pin names\n      image {got.get('ref')}\n      pin   {want}")
            for source, repo, ref in sources:
                label = f"{tool}/{source}"
                if not isinstance(repo, str) or not isinstance(ref, str) or not SHA.fullmatch(ref):
                    bad.append(f"{label}: invalid repository/commit: {repo!r} {ref!r}")
                    continue
                match = FORK_REPO.fullmatch(repo)
                if not match:
                    slug = re.sub(r"^https://github\.com/", "", repo, flags=re.I)
                    slug = re.sub(r"\.git/?$", "", slug).lower()
                    exc = PENDING.get(slug)
                    if exc and not exc["expired"]:
                        declared = want if source == "primary" else args.get(source.upper() + "_REF")
                        if ref != declared:
                            bad.append(f"{label}: exception source differs from its pin {declared!r}")
                        else:
                            deferred.append(f"{label}: built from {repo} "
                                            f"(pending fork, expires {exc.get('expires')})")
                    elif exc:
                        bad.append(f"{label}: exception for {repo} EXPIRED on {exc.get('expires')}")
                    else:
                        bad.append(f"{label}: built from {repo!r}, which is not ours")
                    continue
                name = canonical.get(match.group(1).lower())
                if name is None:
                    bad.append(f"{label}: {repo} has no cloned-source pin")
                elif ref != pins[name]:
                    bad.append(f"{label}: {name} image {ref} differs from pin {pins[name]}")
                else:
                    covered.add(name)
    finally:
        run("docker", "rm", "-f", cid)

    if seen == 0:
        print("check_image_provenance: found no provenance file at all — this "
              "image predates the per-tool split, or every COPY was dropped. "
              "Either way nothing was verified.", file=sys.stderr)
        return 2

    unspoken = sorted(set(pins) - covered)
    if unspoken:
        bad.append("NOT CHECKED — no matching source provenance for: " + ", ".join(unspoken))

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
    print("check_image_provenance: %d/%d records in %s are at the pinned commit"
          % (seen, len(PRIMARY), image))
    print("check_image_provenance: %d of %d pinned fork(s) have verified source "
          "provenance in the image" % (len(covered), len(pins)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
