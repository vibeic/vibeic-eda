#!/usr/bin/env python3
"""openroad-dispatch must not put the current directory on the loader path.

WHAT WAS WRONG
==============
``tools/openroad/openroad-dispatch`` is what LibreLane runs as OpenROAD
(``_LLN_OVERRIDE_OPENROAD``). It sends ``-python`` invocations and LibreLane's
own scripts to ``openroad-python``, and it prepends or-tools to the loader path
first. Before this fix it did that with

    LD_LIBRARY_PATH="/opt/or-tools/lib:${LD_LIBRARY_PATH:-}"

and when LD_LIBRARY_PATH is unset or empty, that gives ``/opt/or-tools/lib:``.
The trailing empty entry is not harmless. glibc reads an empty
LD_LIBRARY_PATH entry as the CURRENT DIRECTORY and searches LD_LIBRARY_PATH
before RUNPATH. So every ``openroad -python`` step would first look for its
shared libraries in whatever directory LibreLane ran it from, which is a run
directory holding design files. The fix is
``/opt/or-tools/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}``.

WHAT THIS TEST ASSERTS
======================
The SHIPPED dispatcher is run under ``sh``, which is dash here and in the image,
with its two absolute targets replaced by stubs that report what they received.
We check every LD_LIBRARY_PATH state a caller can have. The old expression goes
through the same harness and must show the empty entry. If the harness could
not see the defect, a green result for the fix would prove nothing.

We also check that ``tools/openroad/Dockerfile`` names the dispatcher's sha256.
The artefact tag only sees that Dockerfile, so without the digest a dispatcher
edit would keep the old tag and ship nothing.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "tools" / "openroad" / "openroad-dispatch"
RECIPE = ROOT / "tools" / "openroad" / "Dockerfile"

PYTHON_BIN = "/foss/tools/openroad/bin/openroad-python"
TCL_BIN = "/foss/tools/openroad/bin/openroad"
#: The expression this change replaced, verbatim from efa5251.
OLD_EXPR = 'LD_LIBRARY_PATH="/opt/or-tools/lib:${LD_LIBRARY_PATH:-}"'

STUB = """#!/bin/sh
echo "route={route}"
if [ "${{LD_LIBRARY_PATH+set}}" = set ]; then
    echo "llp=$LD_LIBRARY_PATH"
else
    echo "llp-unset"
fi
"""


def _harness(tmp_path: Path, text: str) -> Path:
    """The dispatcher text with its exec targets pointed at reporting stubs."""
    for route in ("python", "tcl"):
        stub = tmp_path / f"stub-{route}"
        stub.write_text(STUB.format(route=route))
        stub.chmod(0o755)
    # PYTHON_BIN first: TCL_BIN is a prefix of it.
    assert text.count(PYTHON_BIN) == 1, "the python route's target moved"
    text = text.replace(PYTHON_BIN, str(tmp_path / "stub-python"))
    assert len(re.findall(re.escape(TCL_BIN) + r'(?=\s)', text)) == 1, (
        "the tcl route's target moved")
    text = re.sub(re.escape(TCL_BIN) + r'(?=\s)', str(tmp_path / "stub-tcl"), text)
    script = tmp_path / "dispatch"
    script.write_text(text)
    return script


def _run(script: Path, args: list[str], llp: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if llp is not None:
        env["LD_LIBRARY_PATH"] = llp
    sh = shutil.which("sh")
    assert sh, "no sh on PATH; the dispatcher's own interpreter is missing"
    p = subprocess.run([sh, str(script), *args], env=env, capture_output=True,
                       text=True, timeout=30, cwd=script.parent)
    assert p.returncode == 0, p.stderr
    out = dict(route=None, llp=None)
    for line in p.stdout.splitlines():
        if line.startswith("route="):
            out["route"] = line.split("=", 1)[1]
        elif line.startswith("llp="):
            out["llp"] = line.split("=", 1)[1]
    return out


@pytest.mark.parametrize("llp,expected", [
    (None, "/opt/or-tools/lib"),
    ("", "/opt/or-tools/lib"),
    ("/a:/b", "/opt/or-tools/lib:/a:/b"),
])
@pytest.mark.parametrize("args", [
    ["-python", "io_place.py"],
    ["-exit", "/x/librelane/scripts/openroad/floorplan.tcl"],
])
def test_python_route_has_no_empty_loader_entry(tmp_path, llp, expected, args):
    got = _run(_harness(tmp_path, DISPATCH.read_text()), args, llp)
    assert got["route"] == "python"
    assert got["llp"] == expected
    assert "" not in got["llp"].split(":"), (
        f"LD_LIBRARY_PATH={got['llp']!r} has an empty entry, which glibc "
        "searches as the current directory before RUNPATH")


@pytest.mark.parametrize("llp", [None, ""])
def test_the_harness_sees_the_defect_in_the_old_expression(tmp_path, llp):
    """The red direction: the replaced expression, same harness, must be caught."""
    text = DISPATCH.read_text()
    new = re.search(r'LD_LIBRARY_PATH="[^"]*"', text).group(0)
    assert new != OLD_EXPR, "the dispatcher still carries the old expression"
    got = _run(_harness(tmp_path, text.replace(new, OLD_EXPR)),
               ["-python", "x.py"], llp)
    assert got["route"] == "python"
    assert "" in got["llp"].split(":"), (
        "the harness did not reproduce the trailing empty entry, so it cannot "
        "tell the fix from the defect")


def test_other_invocations_go_to_the_tcl_binary_untouched(tmp_path):
    got = _run(_harness(tmp_path, DISPATCH.read_text()),
               ["-exit", "-no_splash", "flow.tcl"], None)
    assert got == {"route": "tcl", "llp": None}


def test_the_recipe_pins_the_dispatcher_bytes():
    """The tag only sees tools/openroad/Dockerfile, so it must name the file's digest."""
    digest = hashlib.sha256(DISPATCH.read_bytes()).hexdigest()
    text = RECIPE.read_text()
    m = re.search(r'([0-9a-f]{64})\s+/vibeic-openroad/bin/openroad-dispatch"?\s*\\?\s*\n?'
                  r'\s*\|\s*sha256sum\s+-c\s+-', text)
    assert m, ("tools/openroad/Dockerfile no longer checks the dispatcher with "
               "sha256sum -c, so editing the dispatcher alone would not move the tag")
    assert m.group(1) == digest, (
        "tools/openroad/Dockerfile names a different sha256 than openroad-dispatch "
        f"has now ({digest}). Update that line, then OPENROAD_RECIPE in "
        "docker-bake.hcl and the IMG_OPENROAD tag in the root Dockerfile.")
    assert "COPY openroad-dispatch /vibeic-openroad/bin/openroad-dispatch" in text
