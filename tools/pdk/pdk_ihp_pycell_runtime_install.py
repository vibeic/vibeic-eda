#!/usr/bin/env python3
"""Install the PyCell runtime the shipped ihp PDK's own bootstrap puts on
sys.path but the prebuilt volume does not contain.

vibeic-eda#173.  `libs.tech/klayout/tech/pymacros/autorun.lym` appends
`libs.tech/klayout/python/pycell4klayout-api/source/python` to sys.path and
`python/sg13g2_pycell_lib/__init__.py` then imports `cni.tech`, `cni.dlo` and
`pypreprocessor.pypreprocessor`.  In the volume `python/pycell4klayout-api/` and
`python/pypreprocessor/` are EMPTY directories -- upstream git submodules that
were never initialised when the PDK was cut -- so the library raises
ModuleNotFoundError and `SG13_dev` never registers.

This step copies the two gitlinks the PDK's recorded upstream commit pins, from
tools/pdk/ihp_pycell_runtime/.  See PROVENANCE.md there for the blob ids that
make "the same commit" a measured fact.

Refuses to do anything it cannot state afterwards:

* the bootstrap must exist and must really name the runtime path, and the
  PCell library must really import from it -- the import targets are READ OUT
  of the library's own source, so if upstream renames a module this step fails
  instead of installing a package nothing loads;
* every module the library imports must be present in the source tree;
* each destination must be an empty directory (the delivered shape) -- a
  non-empty one means the volume grew its own copy, and a silent overwrite
  would hide that; a symlink means this is the family that BORROWS the runtime
  (ihp-sg13cmos5l -> ihp-sg13g2), and the owner is where it must be installed;
* the number of files installed must equal --expect, in both directions;
* after copying, every imported module must resolve by FILE from the sys.path
  the bootstrap builds -- resolution only, not execution: executing `cni` needs
  KLayout's `pya`, and that is pdk_pycell_lib_check.py's job.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

#: The path the PDK's bootstrap appends; the install target is its parent tree.
RUNTIME_SUBPATH = "pycell4klayout-api/source/python"

#: The bootstrap spells the path as os.path.join('pycell4klayout-api', 'source',
#: 'python') or as one string; either way the three segments appear in order.
_BOOTSTRAP_NAMES_RUNTIME = re.compile(
    r"pycell4klayout-api['\"/,\s]+source['\"/,\s]+python")

#: `from cni.tech import Tech` -> ("cni", "cni.tech")
_FROM_IMPORT = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", re.M)

#: Which vendored subtree each top-level package lives in, and where under the
#: PDK's python/ directory it is installed.
SUBTREES = {
    "cni": "pycell4klayout-api",
    "pypreprocessor": "pypreprocessor",
}


class InstallError(RuntimeError):
    pass


def _module_file(python_dir: Path, dotted: str) -> Path | None:
    """Resolve `a.b.c` the way the bootstrap's sys.path would, by file only."""
    top = dotted.split(".")[0]
    if top == "cni":
        base = python_dir / RUNTIME_SUBPATH
    else:
        base = python_dir
    rel = Path(*dotted.split("."))
    for cand in (base / rel.with_suffix(".py"), base / rel / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def imported_runtime_modules(pycell_init: Path) -> list[str]:
    """Every `from X.Y import` in the library whose top package is one we ship."""
    text = pycell_init.read_text(encoding="utf-8", errors="replace")
    mods = []
    for dotted in _FROM_IMPORT.findall(text):
        if dotted.split(".")[0] in SUBTREES and dotted not in mods:
            mods.append(dotted)
    return mods


def install(pdk_root: Path, pdk: str, source: Path, expect: int) -> list[str]:
    pdk_dir = (pdk_root / pdk).resolve()
    if not pdk_dir.is_dir():
        raise InstallError(f"no such PDK directory: {pdk_root / pdk}")

    klayout = pdk_dir / "libs.tech" / "klayout"
    bootstrap = klayout / "tech" / "pymacros" / "autorun.lym"
    if not bootstrap.is_file():
        raise InstallError(
            f"{bootstrap} is absent: this PDK has no bootstrap that would put the "
            "runtime on sys.path, so installing it would leave dead code in the volume"
        )
    if not _BOOTSTRAP_NAMES_RUNTIME.search(
            bootstrap.read_text(encoding="utf-8", errors="replace")):
        raise InstallError(
            f"{bootstrap} does not name {RUNTIME_SUBPATH!r}; the volume's bootstrap "
            "changed and this step no longer describes what it needs"
        )

    python_dir = klayout / "python"
    inits = sorted(python_dir.glob("*_pycell_lib/__init__.py"))
    if not inits:
        raise InstallError(f"{python_dir} has no *_pycell_lib/__init__.py to satisfy")
    modules = []
    for init in inits:
        for m in imported_runtime_modules(init):
            if m not in modules:
                modules.append(m)
    if not modules:
        raise InstallError(
            f"{[str(i) for i in inits]} import none of {sorted(SUBTREES)}; the "
            "library changed and this step no longer describes what it needs"
        )

    if not source.is_dir():
        raise InstallError(f"no such source directory: {source}")
    for dotted in modules:
        top = dotted.split(".")[0]
        if _module_file(source, dotted) is None:
            raise InstallError(
                f"the library imports {dotted!r} but {source}/{SUBTREES[top]} does "
                "not provide it"
            )

    plan: list[tuple[Path, Path]] = []
    for top, sub in SUBTREES.items():
        src_tree = source / sub
        if not src_tree.is_dir():
            raise InstallError(f"{src_tree} is absent from the vendored runtime")
        dest_tree = python_dir / sub
        if dest_tree.is_symlink():
            raise InstallError(
                f"{dest_tree} is a symlink to "
                f"{dest_tree.readlink()} -- this family borrows the runtime; "
                "install it in the family that owns the directory"
            )
        src_files = sorted(p for p in src_tree.rglob("*")
                           if p.is_file() and "__pycache__" not in p.parts)
        if dest_tree.exists() and any(dest_tree.iterdir()):
            # Our own copy, byte for byte, is not "the volume grew its own": a
            # derived image built FROM a base that already ran this step
            # reaches here with exactly these files. Anything else is refused.
            dst_files = sorted(p for p in dest_tree.rglob("*")
                               if p.is_file() and "__pycache__" not in p.parts)
            same = ([p.relative_to(dest_tree) for p in dst_files]
                    == [p.relative_to(src_tree) for p in src_files]
                    and all(a.read_bytes() == b.read_bytes()
                            for a, b in zip(src_files, dst_files)))
            if not same:
                raise InstallError(
                    f"{dest_tree} is not empty and is not this runtime byte for "
                    "byte -- the volume now ships its own copy. Remove this step "
                    "rather than overwriting what upstream delivered."
                )
            for f in src_files:
                plan.append((f, dest_tree / f.relative_to(src_tree)))
            continue
        for f in src_files:
            plan.append((f, dest_tree / f.relative_to(src_tree)))

    if len(plan) != expect:
        raise InstallError(
            f"expected {expect} files under {source}, found {len(plan)}"
        )

    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    installed = sorted(
        str(p.relative_to(python_dir))
        for sub in SUBTREES.values()
        for p in (python_dir / sub).rglob("*") if p.is_file()
    )
    if len(installed) != expect:
        raise InstallError(f"installed {len(installed)} files, meant to install {expect}")

    for dotted in modules:
        if _module_file(python_dir, dotted) is None:
            raise InstallError(
                f"after install, {dotted!r} still does not resolve from the path "
                f"the bootstrap builds under {python_dir}"
            )
    return installed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdk-root", type=Path, default=Path("/foss/pdks"))
    ap.add_argument("--pdk", default="ihp-sg13g2")
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--expect", type=int, required=True)
    args = ap.parse_args(argv)

    try:
        installed = install(args.pdk_root, args.pdk, args.source, args.expect)
    except InstallError as exc:
        print(f"[FAIL] ihp pycell runtime install: {exc}", file=sys.stderr)
        return 1
    print(
        f"[PASS] ihp pycell runtime install: {len(installed)} files into "
        f"{args.pdk}/libs.tech/klayout/python/{{{','.join(SUBTREES.values())}}}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
