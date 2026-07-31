#!/usr/bin/env python3
"""A fork recorded as "not in the image" must not be in the image.

Lives HERE and not in vibe-ic: it reads THIS repo's fork ledger, and the defect
it finds — `integrated = bool(ref)` cannot see a tool delivered by any route
other than an `ARG <TOOL>_REF` — is only fixable in `discover_forks`. As a
vibe-ic landing gate it blocked every commit over a condition no commit there
could change, which is the shape that gets a gate deleted rather than fixed.

WHY THIS EXISTS
===============
The fork ledger marks a tool `integrated = False` when the pin resolver cannot
find an `ARG <TOOL>_REF` for it, and renders that as:

    ### <tool>: not_layered — nothing to assess.

with the ledger's own comment defining it as "forked but NOT pinned into the
image — such a tool uses upstream directly, so there is nothing to sync".

Measured against `vibeic-eda:0.2.46` on 2026-07-30, five of the six tools in
that state ARE in the image:

    OpenSTA                /foss/tools/bin/sta
    ciel                   /usr/local/bin/ciel        (Ciel v2.5.1)
    IHP-Open-PDK           /foss/pdks/ihp-sg13g2      (262 MB)
    open_pdks              /foss/pdks/sky130A
    ASAP7_for_KLayout      /foss/pdks/asap7
    OpenROAD-flow-scripts  genuinely absent

ciel is not incidental: `/foss/pdks/sky130A` and `/foss/pdks/gf180mcuD` are
symlinks into `ciel/<pdk>/versions/<sha>/`, so the tool that puts both sign-off
PDKs on disk was recorded as absent and excluded from every assessment.

`integrated = bool(ref)` makes "I could not detect a pin" indistinguishable from
"it is not shipped", and the label asserts the second. That is vibeic-eda#32.

This does NOT change what `integrated` means — that flag gates the assessment
for every tool and is not something to redefine from a checker. It only makes
the contradiction VISIBLE: a claim of absence, tested against the image.

WHAT IT REFUSES TO DO
=====================
* Pass because it could not look. No docker, no image, a container that failed —
  rc 2 with the reason. An unasked question is not a confirmed absence.
* Pass on an empty claim list. Zero tools checked finds zero contradictions.
* Report a tool as contradicted on a path it was never given. A tool with no
  known path in the registry is UNKNOWN and counted separately, because "I have
  nowhere to look" must not read as "I looked and it was gone".

Exit: 0 every absence claim holds, 1 at least one is contradicted, 2 could not check.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Dict, List, Tuple

RC_OK, RC_CONTRADICTED, RC_CANNOT_CHECK = 0, 1, 2

def _default_image() -> str:
    """The image this repo currently ships, read from VERSION.

    Was hard-coded `0.2.46`. VERSION said 0.2.47, so every default run verified
    the absence claims against a PUBLISHED-BUT-SUPERSEDED image — the check
    passed and said nothing about what we ship today. A tool that entered the
    image in 0.2.47 while its ledger entry still claimed absence would not have
    been caught by the gate whose entire job is that contradiction.

    Falls back to the last known-good tag rather than raising: a missing VERSION
    is a reason to say so, not to skip the check entirely.
    """
    v = pathlib.Path(__file__).resolve().parents[1] / "VERSION"
    try:
        tag = v.read_text(encoding="utf-8").strip()
    except OSError:
        tag = ""
    if not tag:
        print(f"WARNING: {v} unreadable; falling back to 0.2.46, which may not "
              f"be what this repo ships", file=sys.stderr)
        tag = "0.2.46"
    return f"ghcr.io/vibeic/vibeic-eda:{tag}"


DEFAULT_IMAGE = _default_image()

#: Where each tool WOULD live if it shipped. A tool absent from this map is
#: reported as unknown rather than assumed absent — see the refusals above.
KNOWN_PATHS: Dict[str, Tuple[str, ...]] = {
    "OpenSTA": ("/foss/tools/bin/sta", "/foss/tools/openroad/bin/sta"),
    "ciel": ("/usr/local/bin/ciel",),
    "IHP-Open-PDK": ("/foss/pdks/ihp-sg13g2",),
    "open_pdks": ("/foss/pdks/sky130A", "/foss/pdks/gf180mcuD"),
    "OpenROAD-flow-scripts": ("/orfs", "/foss/tools/OpenROAD-flow-scripts"),
    "ASAP7_for_KLayout": ("/foss/pdks/asap7", "/foss/tools/asap7"),
    "asap7_pdk_r1p7": ("/foss/pdks/asap7",),
    "asap7sc7p5t_28": ("/foss/pdks/asap7",),
}


def _run(argv: List[str], timeout: int = 180) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "docker not found"
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, "", f"{type(exc).__name__}: {exc}"


def absent_claims(ledger_dir) -> Tuple[List[str], str]:
    """Tools the ledger says are NOT in the image, or ([], reason)."""
    from pathlib import Path
    d = Path(ledger_dir)
    if not d.is_dir():
        return [], f"no ledger directory at {d}"
    out, unreadable = [], []
    for p in sorted(d.glob("*.json")):
        if p.stem == "index":
            continue
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            unreadable.append(p.stem)
            continue
        if not rec.get("integrated"):
            out.append(rec.get("tool") or p.stem)
    if unreadable:
        return [], (f"{len(unreadable)} ledger(s) could not be read "
                    f"({', '.join(unreadable[:4])}); an unreadable ledger makes "
                    f"no claim, and a missing claim is not a confirmed absence")
    return out, ""


#: The image ours is composed FROM. A path present in both, identical, was never
#: ours — see `_ours_only`.
DEFAULT_BASE = "hpretl/iic-osic-tools@sha256:7371bae55da486f492cc270ea6137c4fcf3b11971de7a4506a74f62be143537a"


def _sizes(image: str, paths: List[str]) -> Tuple[Dict[str, str], str]:
    """{path: size-or-'dir'} for the paths that exist, or ({}, reason)."""
    if not paths:
        return {}, ""
    checks = "; ".join(
        f'[ -e "{p}" ] && echo "AT {p} $(stat -Lc%s "{p}" 2>/dev/null || echo dir)"'
        for p in paths)
    rc, out, err = _run(["docker", "run", "--rm", "--entrypoint", "sh",
                         image, "-c", checks + "; echo PROBE_DONE"], timeout=300)
    if rc == 127:
        return {}, "docker is not installed"
    if "PROBE_DONE" not in (out or ""):
        return {}, (f"the probe did not complete on {image} (rc={rc}): "
                    f"{(err or out).strip()[:160]}")
    got = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] == "AT":
            got[f[1]] = f[2]
    return got, ""


def _ours_only(found: Dict[str, str], sizes: Dict[str, str],
               base_image: str) -> Tuple[Dict[str, str], str]:
    """Drop the paths our build did not put there.

    `integrated` claims OUR FORK reaches the image — not that something exists at
    a path. Four of this check's five original findings were the BASE image's
    (`hpretl/iic-osic-tools`): ciel, sky130A, ihp-sg13g2 and, until 0.2.46, sta.
    For those the ledger's `not_layered — uses upstream directly` is ACCURATE,
    and reporting them made the gate mostly wrong (vibeic-eda#32).

    A path is ours when it is absent from the base or differs from it. `sta` is
    the proof this works: it was byte-identical to the base's copy until 0.2.46
    replaced it, so the same predicate reclassifies it without being told.
    """
    if not found:
        return {}, ""
    theirs, err = _sizes(base_image, sorted(set(found.values())))
    if err:
        return {}, err
    # Absent from the base, or present and DIFFERENT. Comparing presence alone
    # would call `sta` inherited — it sits at the same path in both images and
    # is the one case that must come out as ours.
    return ({t: p for t, p in found.items()
             if p not in theirs or theirs[p] != sizes.get(p)}, "")


def probe_image(image: str, tools: List[str]) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """({tool: path}, {path: size}) for tools present in the image, or a reason.

    The sizes come back because `_ours_only` needs to compare them against the
    base image: same path, same size means our build did not put it there.
    """
    paths = sorted({p for t in tools for p in KNOWN_PATHS.get(t, ())})
    if not paths:
        return {}, {}, ""
    sizes, err = _sizes(image, paths)
    if err:
        return {}, {}, err
    found: Dict[str, str] = {}
    for t in tools:
        for p in KNOWN_PATHS.get(t, ()):
            if p in sizes:
                found.setdefault(t, p)
                break
    return found, sizes, ""


def check(image: str, ledger_dir, base_image: str = DEFAULT_BASE) -> dict:
    claimed, err = absent_claims(ledger_dir)
    if err:
        return {"error": err}
    if not claimed:
        # Every tool is integrated. Nothing claims absence, so there is nothing
        # this check can contradict — a real clean state, not an empty scan.
        return {"image": image, "claimed_absent": [], "contradicted": {},
                "unknown_path": [], "confirmed_absent": []}

    known = [t for t in claimed if t in KNOWN_PATHS]
    unknown = sorted(t for t in claimed if t not in KNOWN_PATHS)
    found, sizes, perr = probe_image(image, known)
    if perr:
        return {"error": perr}
    # `integrated` claims OUR FORK reaches the image. A path the BASE image
    # already had at the same size was never ours, and reporting it made four of
    # this check's five original findings wrong (vibeic-eda#32).
    found, berr = _ours_only(found, sizes, base_image)
    if berr:
        return {"error": f"base-image comparison: {berr}"}
    return {"image": image, "claimed_absent": sorted(claimed),
            "contradicted": found,
            "unknown_path": unknown,
            "confirmed_absent": sorted(t for t in known if t not in found)}


def main(argv=None) -> int:
    import os
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--ledger",
                    default=os.path.expanduser(
                        "~/.cache/eda-fork-gatekeeper/ledger"))
    ap.add_argument("--baseline", default=None,
                    help="JSON register of ALREADY-KNOWN contradictions. Only a "
                         "NEW one fails; the recorded set prints every run so it "
                         "stays visible rather than becoming permission.")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help="the image ours is composed FROM; a path identical in "
                         "both was never ours")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    res = check(a.image, a.ledger, a.base)
    if a.json:
        from pathlib import Path
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "fork_presence_claim_check", **res}, indent=2) + "\n",
            encoding="utf-8")

    if "error" in res:
        print(f"[NOT CHECKED] {res['error']}. An absence that could not be "
              f"tested is not a confirmed absence.", file=sys.stderr)
        return RC_CANNOT_CHECK

    if res["unknown_path"]:
        print(f"  {len(res['unknown_path'])} tool(s) claim absence but have no "
              f"known path to test: {', '.join(res['unknown_path'])}. Nowhere to "
              f"look is not the same as looked-and-gone.", file=sys.stderr)

    # A gate that fails on a KNOWN defect nobody can fix from here blocks every
    # landing until someone deletes the gate. The register keeps the existing
    # five visible on every run while letting a NEW one still stop a landing.
    known = set()
    if a.baseline:
        from pathlib import Path
        bp = Path(a.baseline)
        if bp.exists():
            try:
                known = set((json.loads(bp.read_text()).get("contradicted") or {}))
            except (OSError, ValueError) as exc:
                print(f"[NOT CHECKED] baseline {bp} unreadable: {exc}. A register "
                      f"that cannot be read is not an empty register.",
                      file=sys.stderr)
                return RC_CANNOT_CHECK
    new = {t: p for t, p in res["contradicted"].items() if t not in known}
    recorded = len(res["contradicted"]) - len(new)
    if recorded:
        print(f"  {recorded} contradiction(s) recorded as known debt "
              f"(vibeic-eda#32): shipped, but the ledger calls them absent.",
              file=sys.stderr)
    if a.baseline and not new:
        print(f"[PASS] no NEW absence claim is contradicted in {res['image']} "
              f"({recorded} recorded).", file=sys.stderr)
        return RC_OK
    if a.baseline:
        res = {**res, "contradicted": new}

    if res["contradicted"]:
        print(f"[FAIL] {len(res['contradicted'])} fork(s) are recorded as NOT in "
              f"{res['image']} and are in it:", file=sys.stderr)
        for t, p in sorted(res["contradicted"].items()):
            print(f"    {t:24s} {p}", file=sys.stderr)
        print("  The ledger renders these as 'not_layered — nothing to assess', "
              "so each is excluded from every upstream assessment while shipping "
              "to users (vibeic-eda#32).", file=sys.stderr)
        return RC_CONTRADICTED

    n = len(res["confirmed_absent"])
    print(f"[PASS] every absence claim holds"
          f"{f' ({n} verified absent)' if n else ''} in {res['image']}.",
          file=sys.stderr)
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
