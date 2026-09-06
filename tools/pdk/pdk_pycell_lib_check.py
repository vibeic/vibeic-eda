#!/usr/bin/env python3
"""Where a shipped PDK carries a KLayout PCell library, it must import and
register the libraries its own source names.

vibeic-eda#173.  Both ihp families shipped `libs.tech/klayout/python/
<family>_pycell_lib/`, and neither could load: the runtime the library imports
(`cni`, `pypreprocessor`) was an empty submodule directory, so
`SG13_dev` never registered and everything built on it -- the seal ring, every
generated device -- silently drew nothing.  The seal-ring check saw the
symptom; this is the check for the cause, and it is general: any family, any
library, any registered name.

For every shipped PDK family that HAS such a library this asserts, under the
family's own technology (`klayout -n <tech> -zz -r ...`, which runs the PDK's
own bootstrap macro exactly as a user session would):

  0. the technology itself loads -- KLAYOUT_PATH is pointed at the family's
     own libs.tech/klayout for the probe, because the image's default
     environment loads ONE family, and `-n <other>` then falls back silently
     to that one while reporting the other's name (measured: `-n sg13cmos5l`
     under the default environment answered with sg13g2's libraries);
  1. the package imports -- a ModuleNotFoundError is reported with the module
     it names, so the missing piece is stated, not guessed;
  1b. KLayout's macro loader read every file of the library it tried to --
     `ERROR: Reading <file>` against the library's own tree is a FAIL even
     when registration succeeds (measured on ihp-sg13cmos5l: two device
     modules are dangling symlinks into a newer sg13g2 that no longer has them);
  2. every library name the package's own source registers
     (`.register("SG13_dev")`) is present in `pya.Library.library_names()`
     afterwards.  The names are READ OUT of the library, so a renamed or
     added library upstream changes what is asserted, not whether it is.

A family with no PCell library is N/A, not a failure.  A library that
registers nothing this check can name, a family with no technology file, or a
host with no `klayout` on PATH are NOT_MEASURED -- "I could not check" and
"this is broken" are different sentences.  `--require <family>` turns N/A and
NOT_MEASURED into FAIL for the families that must carry a working library.
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

REGISTER_RE = re.compile(r"\.register\(\s*['\"]([^'\"]+)['\"]\s*\)")
TECH_NAME_RE = re.compile(r"<name>([^<]+)</name>")

#: Runs under `klayout -n <tech> -zz -r`, with KLAYOUT_PATH pointing at the
#: family's own libs.tech/klayout, so the PDK's OWN bootstrap macro is what
#: puts the runtime on sys.path -- the probe adds nothing. If the technology
#: did not load, nothing below it was measured, and the probe says so first.
PROBE = r"""
import pya
print("TECH", {tech!r}, pya.Technology.has_technology({tech!r}))
try:
    __import__({pkg!r})
except Exception as exc:
    print("IMPORTFAIL", type(exc).__name__ + ": " + str(exc))
print("LIBS", ",".join(pya.Library.library_names()))
"""

#: KLayout's macro loader reports a file of the library it could not read as
#: `ERROR: Reading <path>: ...`. A library that registers while two of its
#: own device modules failed to load is not a working library.
_LOADER_ERROR = re.compile(r"^ERROR:\s+Reading\s+(\S+?):", re.M)


def _family_env(pdk_dir: Path) -> dict:
    """The environment in which `-n <tech>` resolves to THIS family."""
    kl = pdk_dir / "libs.tech" / "klayout"
    env = dict(os.environ)
    env["KLAYOUT_PATH"] = f"{kl}{os.pathsep}{kl / 'tech'}"
    return env


def discover_pdks(pdk_root: Path) -> list[str]:
    out = []
    for child in sorted(pdk_root.iterdir()):
        if child.name == "ciel" or child.name.startswith("."):
            continue
        if (child / "libs.tech").is_dir():
            out.append(child.name)
    return out


def _tech_name(pdk_dir: Path):
    tech_dir = pdk_dir / "libs.tech" / "klayout" / "tech"
    for lyt in sorted(tech_dir.glob("*.lyt")):
        m = TECH_NAME_RE.search(lyt.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    return None


def registered_names(pycell_init: Path) -> list[str]:
    text = pycell_init.read_text(encoding="utf-8", errors="replace")
    names = []
    for n in REGISTER_RE.findall(text):
        if n not in names:
            names.append(n)
    return names


def check_pdk(pdk_root: Path, pdk: str) -> tuple[str, str]:
    """Return (verdict, detail); verdict in {PASS, FAIL, N/A, NOT_MEASURED}."""
    pdk_dir = (pdk_root / pdk).resolve()
    python_dir = pdk_dir / "libs.tech" / "klayout" / "python"
    inits = sorted(python_dir.glob("*_pycell_lib/__init__.py"))
    if not inits:
        return "N/A", "no libs.tech/klayout/python/*_pycell_lib/__init__.py"

    pkg = inits[0].parent.name
    names = registered_names(inits[0])
    if not names:
        return "NOT_MEASURED", (
            f"{pkg} registers no library this check can name (no "
            ".register('<name>') in its __init__.py)"
        )
    tech = _tech_name(pdk_dir)
    if tech is None:
        return "NOT_MEASURED", (
            f"{pkg} names {names}, but no .lyt under {pdk_dir}/libs.tech/klayout/tech "
            "names a technology to load it with"
        )
    klayout = shutil.which("klayout")
    if klayout is None:
        return "NOT_MEASURED", (
            f"{pkg} names {names}; klayout is not on PATH, so whether they "
            "register was not measured"
        )

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(PROBE.format(tech=tech, pkg=pkg))
        probe = fh.name
    try:
        proc = subprocess.run([klayout, "-n", tech, "-zz", "-r", probe],
                              capture_output=True, text=True, env=_family_env(pdk_dir))
    finally:
        os.unlink(probe)
    out = (proc.stdout or "") + (proc.stderr or "")

    tech_loaded = None
    import_fail = None
    libs: list[str] = []
    for line in out.splitlines():
        if line.startswith("TECH "):
            tech_loaded = line.rstrip().endswith("True")
        elif line.startswith("IMPORTFAIL "):
            import_fail = line[len("IMPORTFAIL "):].strip()
        elif line.startswith("LIBS "):
            libs = [x for x in line[len("LIBS "):].strip().split(",") if x]
    if tech_loaded is None:
        return "FAIL", f"probe produced no verdict under technology {tech} (exit {proc.returncode})"
    if not tech_loaded:
        return "NOT_MEASURED", (
            f"technology {tech!r} did not load from {pdk_dir}/libs.tech/klayout, so "
            f"nothing about {pkg} was measured"
        )
    unreadable = sorted({
        os.path.basename(m) for m in _LOADER_ERROR.findall(out)
        if os.path.abspath(m).startswith(str(python_dir))
    })
    if import_fail is not None:
        return "FAIL", f"{pkg} does not import under technology {tech}: {import_fail}"
    if unreadable:
        return "FAIL", (
            f"KLayout could not read {len(unreadable)} of {pkg}'s own files under "
            f"technology {tech}: {', '.join(unreadable)}"
            + (f" (the library still registered {sorted(libs)})" if libs else "")
        )
    missing = [n for n in names if n not in libs]
    if missing:
        return "FAIL", (
            f"{pkg} imports under technology {tech} but registered "
            f"{sorted(libs) or 'nothing'}; its source names {missing} and none of those is present"
        )
    return "PASS", f"{pkg} registers {names} under technology {tech}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdk-root", type=Path, default=Path("/foss/pdks"))
    ap.add_argument("--pdk", action="append", dest="pdks", default=None)
    ap.add_argument("--require", action="append", dest="require", default=None,
                    help="PDK family that MUST have a working PCell library; "
                         "N/A and NOT_MEASURED become failures for these. Repeatable.")
    args = ap.parse_args(argv)

    if not args.pdk_root.is_dir():
        print(f"[NOT CHECKED] pycell: no such PDK root {args.pdk_root}", file=sys.stderr)
        return 2
    pdks = args.pdks or discover_pdks(args.pdk_root)
    if not pdks:
        print(f"[NOT CHECKED] pycell: no PDK families under {args.pdk_root}", file=sys.stderr)
        return 2

    required = set(args.require or [])
    failed = False
    for pdk in pdks:
        verdict, detail = check_pdk(args.pdk_root, pdk)
        if verdict in ("N/A", "NOT_MEASURED") and pdk in required:
            verdict, detail = "FAIL", f"required to carry a PCell library, but {detail}"
        if verdict == "FAIL":
            failed = True
        print(f"[{verdict}] pycell {pdk}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
