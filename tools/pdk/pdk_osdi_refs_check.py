#!/usr/bin/env python3
"""Every OSDI object a shipped PDK asks ngspice to load must actually exist.

vibeic-eda#172.  `ihp-sg13g2/libs.tech/ngspice/.spiceinit` ends with

    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/cap_cmomi.osdi'
    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/cap_cmomf.osdi'

and neither file was in the image.  ngspice does not fail the run for a missing
OSDI object -- the model is simply not there -- so the first symptom is a design
using the MoM capacitor family silently having no such device.  The PDK states
the requirement itself; nothing was checking that the image met it.

What is scanned: the ngspice loader directive `osdi` / `pre_osdi` at the start of
a line, in the PDK's own spice control and model files.  Comment lines (`*`) are
skipped on purpose -- a path quoted in prose is documentation, not a load.

`$PDK_ROOT` and `$PDK` are expanded per PDK being checked, not from the ambient
environment, so the answer does not depend on which PDK the caller happened to
have selected.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCAN_SUFFIXES = {".lib", ".spice", ".cir", ".sp", ".ngspice", ".init"}
SCAN_NAMES = {".spiceinit", "spinit"}
DIRECTIVE = re.compile(r"^\s*(?:pre_)?osdi\s+(.+?)\s*$", re.IGNORECASE)


def _iter_scan_files(pdk_dir: Path):
    tech = pdk_dir / "libs.tech"
    if not tech.is_dir():
        return
    for path in tech.rglob("*"):
        if not path.is_file():
            continue
        if path.name in SCAN_NAMES or path.suffix.lower() in SCAN_SUFFIXES:
            yield path


def _expand(raw: str, pdk_root: Path, pdk: str) -> str:
    token = raw.strip().strip("'\"")
    token = token.replace("$PDK_ROOT", str(pdk_root)).replace("${PDK_ROOT}", str(pdk_root))
    token = token.replace("$PDK", pdk).replace("${PDK}", pdk)
    return token


def check_pdk(pdk_root: Path, pdk: str) -> list[tuple[Path, int, str, str]]:
    """Return the list of unresolved references as (file, lineno, raw, resolved)."""
    pdk_dir = (pdk_root / pdk).resolve()
    missing: list[tuple[Path, int, str, str]] = []
    for path in _iter_scan_files(pdk_dir):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith("*"):
                continue
            m = DIRECTIVE.match(line)
            if not m:
                continue
            resolved = _expand(m.group(1), pdk_root, pdk)
            if not resolved.endswith(".osdi"):
                continue
            if not Path(resolved).is_file():
                missing.append((path, lineno, m.group(1).strip(), resolved))
    return missing


def discover_pdks(pdk_root: Path) -> list[str]:
    out = []
    for child in sorted(pdk_root.iterdir()):
        if child.name == "ciel" or child.name.startswith("."):
            continue
        if (child / "libs.tech").is_dir():
            out.append(child.name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdk-root", type=Path, default=Path("/foss/pdks"))
    ap.add_argument("--pdk", action="append", dest="pdks", default=None,
                    help="PDK family to check; repeatable. Default: every shipped family.")
    args = ap.parse_args(argv)

    if not args.pdk_root.is_dir():
        print(f"[NOT CHECKED] osdi refs: no such PDK root {args.pdk_root}", file=sys.stderr)
        return 2

    pdks = args.pdks or discover_pdks(args.pdk_root)
    if not pdks:
        print(f"[NOT CHECKED] osdi refs: no PDK families under {args.pdk_root}", file=sys.stderr)
        return 2

    failed = False
    for pdk in pdks:
        missing = check_pdk(args.pdk_root, pdk)
        if missing:
            failed = True
            print(f"[FAIL] osdi refs: {pdk} names {len(missing)} OSDI object(s) that do not exist:",
                  file=sys.stderr)
            for path, lineno, raw, resolved in missing:
                print(f"  {path}:{lineno}: {raw} -> {resolved}", file=sys.stderr)
        else:
            print(f"[PASS] osdi refs: {pdk} -- every OSDI object its libs load resolves")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
