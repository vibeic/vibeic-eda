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
during its own build, and this asserts the composed image carries exactly the
eight expected files, each naming a `vibeic/` repo, each at the commit its pin
names.

Usage:  check_image_provenance.py <image>

Exit: 0 all eight agree, 1 a disagreement, 2 the image could not be inspected
(which is not a pass).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: tool -> the ARG in tools/<tool>/Dockerfile that its provenance must report.
#: Multi-source artefacts report the first; the second is checked via the tag.
PRIMARY = {
    "openroad": "OPENROAD_REF", "yosys": "YOSYS_REF",
    "sat-solvers": "KISSAT_REF", "ngspice": "NGSPICE_REF",
    "lvs": "MAGIC_REF", "iverilog": "IVERILOG_REF",
    "klayout": "KLAYOUT_REF", "verilator": "VERILATOR_REF",
    "gtkwave": "GTKWAVE_REF", "xschem": "XSCHEM_REF",
    "slang": "SLANG_REF", "xyce": "XYCE_REF",
}
ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)

# The dated exceptions are shared with check_fork_only, deliberately. If this
# check applied the rule more strictly than the one that gates the source, a
# source could pass the file check and then fail the image check for the same
# reason — and the obvious way out of that would be to weaken one of them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fork_only import pending as _pending  # noqa: E402


def run(*cmd) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


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
    try:
        for tool, key in PRIMARY.items():
            want = ARG.findall((ROOT / "tools" / tool / "Dockerfile").read_text())
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
