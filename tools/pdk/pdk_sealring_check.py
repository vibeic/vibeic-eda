#!/usr/bin/env python3
"""Where a shipped PDK carries a seal-ring script, the library it imports must load.

vibeic-eda#170.  The volume shipped `libs.tech/klayout/tech/scripts/sealring.py`
without `libs.tech/klayout/tech/pymacros/sealring_cells/`.  The script catches
its own ImportError and calls `sys.exit()` with a printed message and status 0 --
so nothing downstream sees a failure, the ring is simply never drawn, and the die
goes to tapeout_precheck unsealed.  A check has to look past the exit status.

For every shipped PDK family that HAS such a script this asserts:

  1. the package the script names really imports, from the directory the script
     itself puts on sys.path -- the import target is read out of the script, so
     renaming it upstream cannot leave this check passing on a stale name;
  2. the library object it names is constructible, i.e. the PCell declaration
     registers.  A directory of .py files that imports but cannot build the
     library would satisfy (1) and still draw nothing.

A family with no seal-ring script is reported as such and is not a failure --
not every PDK carries one, and inventing a requirement for those would be a
false red.

TWO SHAPES, because the shipped PDKs really do use two.  An early draft of this
check knew only the first and called the other two families FAIL for "imports no
seal-ring package this check can name", which is a statement about the check and
not about the PDK.  That is the false red this docstring exists to prevent.

  package shape  (gf180mcuD)  the script does
                              `from sealring_cells import gf180mcu_sealring`
                              against a directory it puts on sys.path.
  library shape  (ihp-*)      the script names a registered KLayout library and
                              PCell (`LIB = 'SG13_dev'`, `PCELL = 'sealring'`)
                              and is run as `klayout -n <tech> -zz -r ...`.
                              Verified by asking KLayout, under that technology,
                              whether the library registered and carries the PCell.

If a script matches neither shape the verdict is NOT_MEASURED, never FAIL: "I do
not know how to check this" and "this is broken" are different sentences.

CORRECTED 2026-09-07 (vibeic-eda#173).  The library shape had two defects that
made it call a WORKING ihp library FAIL: it asked `library_by_name(lib)` without
the technology, which is None for a technology-bound library; and it ran
`klayout -n <tech>` in the caller's environment, where only the default family
is on KLAYOUT_PATH, so `-n sg13cmos5l` silently answered with sg13g2's state.
Both are fixed in LIB_PROBE / _family_env below; the earlier "ihp FAIL" lines
in 0.3.46/0.3.47 build logs were therefore overdetermined -- the runtime WAS
missing (#173), and the probe could not have seen it present either way.

WHY THIS DOES NOT DO THE OBVIOUS THING.  vibeic-eda#170 asks for a check that
`python3 -c 'import sealring'` resolves.  MEASURED, that check cannot fail:

    0.3.46:  Error: Couldn't load the seal ring library.   rc=0
    0.3.47:  (silent)                                      rc=0

The shipped script's except-branch ends in a bare `sys.exit()`, and `SystemExit`
with no argument is status 0.  So importing it exits 0 whether the library is
there or not, and the only difference is a line on stdout.  Anything built on
that exit status -- including the check the issue asked for -- is vacuous.

That is why this check imports the PACKAGE directly and CONSTRUCTS the library
object, rather than running the script and believing its status.  A missing
package raises ModuleNotFoundError in the probe, which is a real non-zero.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMPORT_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+([A-Za-z_]\w*)", re.M)
LIB_RE = re.compile(r"^\s*LIB\s*=\s*['\"]([^'\"]+)['\"]", re.M)
PCELL_RE = re.compile(r"^\s*PCELL\s*=\s*['\"]([^'\"]+)['\"]", re.M)
TECH_NAME_RE = re.compile(r"<name>([^<]+)</name>")

PROBE = r"""
import sys
sys.path.insert(1, {pymacros!r})
mod = __import__({pkg!r}, fromlist=[{obj!r}])
lib = getattr(mod, {obj!r})
inst = lib()
print("OK", {pkg!r}, {obj!r}, type(inst).__name__)
"""

LIB_PROBE = r"""
import pya
print("TECH", pya.Technology.has_technology({tech!r}))
# A library registered FOR a technology is invisible to library_by_name(name)
# alone; it must be asked for under that technology. Measured 2026-09-07 on
# ihp-sg13g2 with the PyCell runtime installed: library_names() listed SG13_dev,
# library_by_name("SG13_dev") was None, library_by_name("SG13_dev", "sg13g2")
# carried the sealring PCell. Without the technology this probe called a
# working library NOLIB.
lib = pya.Library.library_by_name({lib!r}, {tech!r}) or pya.Library.library_by_name({lib!r})
if lib is None:
    print("NOLIB")
else:
    decl = lib.layout().pcell_declaration({pcell!r})
    print("OK" if decl is not None else "NOPCELL")
"""


def _family_env(pdk_dir):
    """`-n <tech>` resolves only if KLAYOUT_PATH carries the family. The image's
    default environment carries ONE family; asked for another, KLayout falls
    back silently and answers about the wrong one."""
    kl = pdk_dir / "libs.tech" / "klayout"
    env = dict(os.environ)
    env["KLAYOUT_PATH"] = f"{kl}{os.pathsep}{kl / 'tech'}"
    return env


def _consumer(pdk_dir: Path) -> Path:
    return pdk_dir / "libs.tech" / "klayout" / "tech" / "scripts" / "sealring.py"


def discover_pdks(pdk_root: Path) -> list[str]:
    out = []
    for child in sorted(pdk_root.iterdir()):
        if child.name == "ciel" or child.name.startswith("."):
            continue
        if (child / "libs.tech").is_dir():
            out.append(child.name)
    return out


def _tech_name(pdk_dir):
    tech_dir = pdk_dir / "libs.tech" / "klayout" / "tech"
    for lyt in sorted(tech_dir.glob("*.lyt")):
        m = TECH_NAME_RE.search(lyt.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    return None


def _check_package_shape(pdk_dir, pkg, obj):
    pymacros = pdk_dir / "libs.tech" / "klayout" / "tech" / "pymacros"
    if not pymacros.is_dir():
        return "FAIL", f"{pymacros} absent, but sealring.py puts it on sys.path"
    code = PROBE.format(pymacros=str(pymacros), pkg=pkg, obj=obj)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        detail = tail[-1] if tail else f"exit {proc.returncode}"
        return "FAIL", f"`from {pkg} import {obj}` does not resolve under {pymacros}: {detail}"
    return "PASS", f"package shape: {proc.stdout.strip()}"


def _check_library_shape(pdk_dir, lib, pcell):
    tech = _tech_name(pdk_dir)
    if tech is None:
        return "NOT_MEASURED", (
            f"script names library {lib!r}/PCell {pcell!r}, but no .lyt under "
            f"{pdk_dir}/libs.tech/klayout/tech names a technology to load it with"
        )
    klayout = shutil.which("klayout")
    if klayout is None:
        return "NOT_MEASURED", (
            f"script names library {lib!r}/PCell {pcell!r}; klayout is not on PATH, "
            "so whether that library registers was not measured"
        )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(LIB_PROBE.format(lib=lib, pcell=pcell, tech=tech))
        probe = fh.name
    try:
        proc = subprocess.run([klayout, "-n", tech, "-zz", "-r", probe],
                              capture_output=True, text=True, env=_family_env(pdk_dir))
    finally:
        os.unlink(probe)
    out = (proc.stdout or "") + (proc.stderr or "")
    if "TECH False" in out:
        return "NOT_MEASURED", (
            f"technology {tech!r} did not load from {pdk_dir}/libs.tech/klayout, so "
            f"whether library {lib!r} registers was not measured"
        )
    if "OK" in out.split():
        return "PASS", f"library shape: {lib}/{pcell} registered under technology {tech}"
    reason = ""
    for line in out.splitlines():
        if "ModuleNotFoundError" in line or "ERROR" in line:
            reason = line.strip()
            break
    if "NOLIB" in out:
        what = f"library {lib!r} did not register under technology {tech}"
    elif "NOPCELL" in out:
        what = f"library {lib!r} registered but carries no PCell {pcell!r}"
    else:
        what = f"probe produced no verdict under technology {tech} (exit {proc.returncode})"
    return "FAIL", what + (f"; first error: {reason}" if reason else "")


def check_pdk(pdk_root: Path, pdk: str) -> tuple[str, str]:
    """Return (verdict, detail); verdict in {PASS, FAIL, N/A, NOT_MEASURED}."""
    pdk_dir = (pdk_root / pdk).resolve()
    script = _consumer(pdk_dir)
    if not script.is_file():
        return "N/A", "no libs.tech/klayout/tech/scripts/sealring.py"

    text = script.read_text(encoding="utf-8", errors="replace")

    targets = [(pkg, obj) for pkg, obj in IMPORT_RE.findall(text) if "seal" in pkg.lower()]
    if targets:
        pkg, obj = targets[0]
        return _check_package_shape(pdk_dir, pkg, obj)

    lib_m, pcell_m = LIB_RE.search(text), PCELL_RE.search(text)
    if lib_m and pcell_m:
        return _check_library_shape(pdk_dir, lib_m.group(1), pcell_m.group(1))

    return "NOT_MEASURED", (
        f"{script} matches neither the package shape (from <pkg> import <obj>) nor "
        "the library shape (LIB=/PCELL=); this check does not know how to verify it"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdk-root", type=Path, default=Path("/foss/pdks"))
    ap.add_argument("--pdk", action="append", dest="pdks", default=None)
    ap.add_argument("--require", action="append", dest="require", default=None,
                    help="PDK family that MUST have a working seal-ring library; "
                         "N/A becomes a failure for these. Repeatable.")
    args = ap.parse_args(argv)

    if not args.pdk_root.is_dir():
        print(f"[NOT CHECKED] sealring: no such PDK root {args.pdk_root}", file=sys.stderr)
        return 2

    pdks = args.pdks or discover_pdks(args.pdk_root)
    if not pdks:
        print(f"[NOT CHECKED] sealring: no PDK families under {args.pdk_root}", file=sys.stderr)
        return 2

    required = set(args.require or [])
    failed = False
    for pdk in pdks:
        verdict, detail = check_pdk(args.pdk_root, pdk)
        if verdict in ("N/A", "NOT_MEASURED") and pdk in required:
            verdict, detail = "FAIL", f"required to carry a seal ring, but {detail}"
        if verdict == "FAIL":
            failed = True
            print(f"[FAIL] sealring: {pdk}: {detail}", file=sys.stderr)
        else:
            print(f"[{verdict}] sealring: {pdk}: {detail}")

    for pdk in sorted(required - set(pdks)):
        failed = True
        print(f"[FAIL] sealring: {pdk} was required but is not shipped", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
