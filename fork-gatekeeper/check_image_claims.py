#!/usr/bin/env python3
"""What the image says about itself, checked against what it is.

WHY THIS EXISTS (vibeic-eda#28)
===============================
Two kinds of claim ship inside every image and nothing verified either.

**PDK provenance.** Three PDK upstreams are read by every sign-off run, are not
forked, and were named nowhere in this repository: `RTimothyEdwards/open_pdks`
(produces sky130A and gf180mcuD — every DRC and LVS verdict is computed against
its `libs.tech`), `IHP-GmbH/IHP-Open-PDK`, `fossi-foundation/ciel`. All three
404 under `vibeic/`.

No existing guard has jurisdiction, and that is the point rather than an
oversight: `check_fork_only` checks what we CLONE, and these arrive
pre-installed in the base image. Every guard we own is scoped to sources the
build clones, so a dependency that arrives inside the base is invisible to all
of them by construction.

A PDK is not a lesser dependency. A change in open_pdks moves sign-off results
the same way a change in klayout's LEF/DEF importer does — which is exactly what
#17 turned out to be.

**The tool manifest.** `/foss/pdks/versions.txt` is the BASE image's list, and
our image replaces several of the tools it names. Measured on 0.2.45:

    klayout   versions.txt 0.30.5    actual 0.30.10
    magic     versions.txt 8.3.589   actual 8.3.678
    ngspice   versions.txt 43        actual 46

A file we publish, telling users the wrong version of tools we deliberately
replaced. Nobody was comparing it to anything.

WHAT THIS DOES NOT DECIDE
=========================
Whether to mirror those three upstreams or keep taking the base's copy. That is
the owner's call and #28 states both options. What it refuses to allow is a
THIRD state — used, undeclared, and unnoticed — which is where all three were.
`status: upstream` in PDKS.json is a decision someone can point at; silence was
not.

Exit: 0 every claim holds, 1 a claim is wrong or a PDK is undeclared, 2 nothing
compared.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

RC_OK, RC_DRIFT, RC_NOTHING = 0, 1, 2

DIR = Path(__file__).resolve().parent

#: How to ask each tool its version, and which token of the answer to take.
#: Derived from the real binaries rather than a table of expected strings — an
#: expected-version table is the same drift one level over.
#: The probe table lives in ONE file, shared with
#: `tools/refresh_versions_manifest.sh`, which rewrites the manifest at compose
#: time. Two separate tables would let the refresher correct a tool this check
#: does not inspect (silently useless) or let this check police a tool the
#: refresher never touches (permanently red) -- the vibeic-eda#93 shape, a check
#: and the thing it checks drifting apart.
_PROBE_TABLE = Path(__file__).resolve().parent.parent / "tools" / "tool_version_probes.tsv"


def _load_probes(path: Path = _PROBE_TABLE):
    """tool -> (command, regex). Missing/empty table raises, deliberately.

    Returning {} would make `manifest_findings` compare nothing and report no
    findings, which is indistinguishable from every claim holding. If the table
    cannot be read, the answer is "not compared", not "fine".
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing; the version claims cannot be checked, which is "
            f"NOT the same as their holding")
    out = {}
    for ln in path.read_text().splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) != 3:
            raise ValueError(f"{path}: expected 3 tab-separated fields, got "
                             f"{len(parts)}: {ln!r}")
        out[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    if not out:
        raise ValueError(f"{path} names no tools; nothing would be compared")
    return out


_TOOL_PROBE = _load_probes()


def _sh(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def _in_image(image: str, script: str):
    """Run `script` in the image and return its output WITHOUT the base's banner.

    The base's profile.d prints `[INFO] Final PATH variable: …` and two more
    lines to STDOUT on every login shell, and a login shell is required for the
    tools to be on PATH at all. Filtering that here rather than at each call
    site is not tidiness — it is a correctness fix.

    Both halves of the bug it caused are worth keeping in view. `cat SOURCES |
    head -1` returned the banner followed by the real line, so the value this
    program DISPLAYED was `[INFO] Final PATH variable: …` — useless as evidence.
    And the shape check `re.search("open_pdks [0-9a-f]{40}", v)` still passed,
    because the real line was further down the same blob. A verdict that is
    right for the wrong reason survives exactly until the input changes shape.
    """
    rc, out, err = _sh(["docker", "run", "--rm", "--entrypoint", "bash", image,
                        "-lc", script])
    keep = "\n".join(ln for ln in out.splitlines()
                     if not ln.startswith("[INFO]"))
    return rc, keep, err


def declared(root: Path = DIR) -> List[dict]:
    f = root / "PDKS.json"
    if not f.is_file():
        return []
    return json.loads(f.read_text())["pdks"]


def pdk_findings(image: str, decl: List[dict]) -> List[dict]:
    """Every PDK in the image must be declared, and every declared one present.

    Both directions matter. An undeclared PDK is a dependency nobody chose; a
    declared one that vanished is a flow that will fail on a PDK the docs
    promise.
    """
    rc, out, _ = _in_image(image, "ls -1 /foss/pdks 2>/dev/null")
    present = {ln.strip() for ln in out.splitlines()
               if ln.strip() and not ln.startswith("[INFO")
               and "." not in ln.strip()}
    if rc != 0 or not present:
        return [{"kind": "not_checked",
                 "problem": f"could not list /foss/pdks in {image}"}]

    by_name = {d["name"]: d for d in decl}
    findings = []
    for name in sorted(present - set(by_name)):
        findings.append({
            "kind": "undeclared", "pdk": name,
            "problem": "ships in the image and is declared nowhere — no guard "
                       "has jurisdiction over it, because every other guard "
                       "checks what we CLONE and this arrives in the base"})
    for name in sorted(set(by_name) - present):
        findings.append({
            "kind": "missing", "pdk": name,
            "problem": "declared in PDKS.json and not in the image"})

    # Read back the version each one records, so provenance is a fact rather
    # than a promise. A PDK whose version file has gone is reported: it means
    # the next drift will be invisible.
    for name in sorted(present & set(by_name)):
        d = by_name[name]
        if not d.get("version_file"):
            continue
        _, vout, _ = _in_image(
            image, f"cat /foss/pdks/{name}/{d['version_file']} 2>/dev/null | head -1")
        v = vout.strip()
        if not v:
            findings.append({
                "kind": "no_version", "pdk": name,
                "problem": f"declares {d['version_file']} as its version file "
                           f"and the image has none — the next change to this "
                           f"PDK would be undetectable"})
            continue
        pat = d.get("version_pattern")
        if pat and not re.search(pat, v):
            findings.append({
                "kind": "version_shape", "pdk": name, "read": v[:60],
                "problem": f"version does not match the declared shape {pat!r}"})
        else:
            findings.append({"kind": "ok", "pdk": name, "read": v[:60],
                             "upstream": d["upstream"], "status": d["status"]})
    return findings


def manifest_findings(image: str) -> List[dict]:
    """`/foss/pdks/versions.txt` vs the tools actually on PATH.

    The manifest belongs to the base image. We replace tools it names and never
    touched it, so it ships with our image telling users the base's versions.
    """
    _, man, _ = _in_image(image, "cat /foss/pdks/versions.txt 2>/dev/null")
    if not man.strip():
        return [{"kind": "not_checked",
                 "problem": "the image has no /foss/pdks/versions.txt"}]
    claims = {}
    for ln in man.splitlines():
        parts = ln.split()
        if len(parts) >= 2:
            claims[parts[0].lower()] = parts[1]

    findings = []
    for tool, (cmd, pat) in _TOOL_PROBE.items():
        if tool not in claims:
            continue
        _, out, err = _in_image(image, f"{cmd} 2>&1 | head -3")
        m = re.search(pat, out + err)
        if not m:
            findings.append({"kind": "unprobed", "tool": tool,
                             "problem": f"`{cmd}` gave no version to compare"})
            continue
        actual, claimed = m.group(1), claims[tool].lstrip("v")
        if not actual.startswith(claimed) and not claimed.startswith(actual):
            findings.append({
                "kind": "manifest_wrong", "tool": tool,
                "claimed": claimed, "actual": actual,
                "problem": "versions.txt is the BASE image's and we replaced "
                           "this tool; the file ships with our image saying "
                           "the wrong version"})
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--json", default=None)
    ap.add_argument("--strict-manifest", action="store_true",
                    help="fail on a wrong versions.txt entry as well")
    a = ap.parse_args(argv)

    decl = declared()
    if not decl:
        print("[NOT CHECKED] no PDKS.json — nothing was compared, which is not "
              "a clean result", file=sys.stderr)
        return RC_NOTHING

    pdks = pdk_findings(a.image, decl)
    manifest = manifest_findings(a.image)

    ok = [f for f in pdks if f["kind"] == "ok"]
    bad = [f for f in pdks if f["kind"] not in ("ok",)]

    print(f"check_image_claims: {len(decl)} PDK(s) declared, {len(ok)} with a "
          f"readable version, {len(bad)} finding(s); "
          f"{len(manifest)} manifest finding(s)")
    for f in ok:
        print(f"  {f['pdk']:<16} {f['status']:<9} {f['read']}")
    for f in bad:
        print(f"  {f.get('pdk', '*'):<16} {f['kind'].upper():<14} {f['problem']}")
    for f in manifest:
        if f["kind"] == "manifest_wrong":
            print(f"  versions.txt     {f['tool']:<10} says {f['claimed']}, "
                  f"the image runs {f['actual']}")
        else:
            print(f"  versions.txt     {f['kind'].upper():<14} {f['problem']}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "check_image_claims", "image": a.image,
             "pdks": pdks, "manifest": manifest}, indent=2) + "\n",
            encoding="utf-8")

    wrong_manifest = [f for f in manifest if f["kind"] == "manifest_wrong"]
    if bad:
        print(f"[FAIL] {len(bad)} PDK finding(s). An undeclared PDK is a "
              f"dependency nobody chose — and no other guard can see it, "
              f"because they all check what we clone.", file=sys.stderr)
        return RC_DRIFT
    if wrong_manifest and a.strict_manifest:
        print(f"[FAIL] versions.txt misreports {len(wrong_manifest)} tool(s) "
              f"we replaced.", file=sys.stderr)
        return RC_DRIFT
    if wrong_manifest:
        print(f"[PASS] every PDK is declared and its provenance readable. "
              f"versions.txt misreports {len(wrong_manifest)} tool(s) we "
              f"replaced — reported, not failed on, until the file is either "
              f"regenerated or removed (it is the base's, not ours).")
        return RC_OK
    print("[PASS] every PDK the image ships is declared, and every declared "
          "one is present with the version shape it promises")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
