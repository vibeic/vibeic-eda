#!/usr/bin/env python3
"""Install the `sealring_cells` KLayout PCell library the shipped PDK's own
`sealring.py` imports but the prebuilt volume does not contain.

vibeic-eda#170.  `libs.tech/klayout/tech/scripts/sealring.py` is present in the
volume and does:

    sys.path.insert(1, <PDK>/libs.tech/klayout/tech/pymacros)
    from sealring_cells import gf180mcu_sealring

`pymacros/` is there; `pymacros/sealring_cells/` is not, so the script prints
"Error: Couldn't load the seal ring library." and exits.  Every die is then
unsealed, which is what turned General.SealRing into FAIL at tapeout_precheck.

This step copies the matching half of the SAME upstream commit into the volume.
See tools/pdk/sealring_cells/PROVENANCE.md for the sha256 that establishes that
"matching" is a measured fact and not a hope.

Refuses to do anything it cannot state afterwards:

* the consumer script must exist and must really import this package -- if the
  volume ever ships its own copy, or renames the import, this step FAILS instead
  of installing a package nothing loads;
* the destination must not already exist -- a silent overwrite would hide the
  volume having grown its own copy;
* the number of files installed must equal --expect, in both directions.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

IMPORT_LINE = "from sealring_cells import gf180mcu_sealring"


class InstallError(RuntimeError):
    pass


def install(pdk_root: Path, pdk: str, source: Path, expect: int) -> list[str]:
    pdk_dir = (pdk_root / pdk).resolve()
    if not pdk_dir.is_dir():
        raise InstallError(f"no such PDK directory: {pdk_root / pdk}")

    tech = pdk_dir / "libs.tech" / "klayout" / "tech"
    consumer = tech / "scripts" / "sealring.py"
    if not consumer.is_file():
        raise InstallError(
            f"{consumer} is absent: this PDK has no seal-ring script to satisfy, "
            "so installing the library would leave dead code in the volume"
        )
    text = consumer.read_text(encoding="utf-8", errors="replace")
    if IMPORT_LINE not in text:
        raise InstallError(
            f"{consumer} does not contain {IMPORT_LINE!r}; the volume's script "
            "changed and this step no longer describes what it needs"
        )

    pymacros = tech / "pymacros"
    if not pymacros.is_dir():
        raise InstallError(f"{pymacros} is absent; sealring.py puts it on sys.path")

    if not source.is_dir():
        raise InstallError(f"no such source directory: {source}")
    names = sorted(p.name for p in source.glob("*.py"))
    if "__init__.py" not in names:
        raise InstallError(f"{source} has no __init__.py; it is not a package")
    if len(names) != expect:
        raise InstallError(
            f"expected {expect} .py files in {source}, found {len(names)}: {names}"
        )

    dest = pymacros / "sealring_cells"
    if dest.exists():
        # Our own copy, byte for byte, is not "the volume grew its own": a
        # derived image built FROM a base that already ran this step (the
        # 2026-09-07 lineage, 0.3.47 -> 0.3.48) reaches here with exactly the
        # files it would install. Anything else -- a file more, fewer, or
        # different -- is still refused, unread and unoverwritten.
        present = sorted(p.name for p in dest.glob("*")) if dest.is_dir() else None
        same = (present == names and all(
            (dest / n).is_file() and (dest / n).read_bytes() == (source / n).read_bytes()
            for n in names))
        if same:
            return names
        raise InstallError(
            f"{dest} already exists and is not this package byte for byte -- "
            "the volume now ships its own copy. Remove this step rather than "
            "overwriting what upstream delivered."
        )

    dest.mkdir()
    for name in names:
        shutil.copy2(source / name, dest / name)

    installed = sorted(p.name for p in dest.glob("*.py"))
    if installed != names:
        raise InstallError(f"installed {installed}, meant to install {names}")
    return installed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdk-root", type=Path, default=Path("/foss/pdks"))
    ap.add_argument("--pdk", default="gf180mcuD")
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--expect", type=int, required=True)
    args = ap.parse_args(argv)

    try:
        installed = install(args.pdk_root, args.pdk, args.source, args.expect)
    except InstallError as exc:
        print(f"[FAIL] sealring_cells install: {exc}", file=sys.stderr)
        return 1
    print(
        f"[PASS] sealring_cells install: {len(installed)} files into "
        f"{args.pdk}/libs.tech/klayout/tech/pymacros/sealring_cells: "
        + ", ".join(installed)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
