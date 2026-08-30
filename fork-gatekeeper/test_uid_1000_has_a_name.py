#!/usr/bin/env python3
"""The uid the image runs as must have a passwd entry (#145).

The Dockerfile says `USER 1000` twice, and until this test nothing created that
account -- `getent passwd 1000` was ABSENT in our image and in the
hpretl/iic-osic-tools base it inherits from. A uid with no passwd entry is not
cosmetic:

    >>> getpass.getuser()
    KeyError: 'getpwuid(): uid not found: 1000'

and `id -un` prints the bare uid to stdout while exiting NON-ZERO, so the
ordinary shell fallback `USER=$(id -un) || USER=designer` captures BOTH arms and
produces the literal '1000\\ndesigner'. That embedded newline is invisible in
normal output, which is why it survived across releases.

Two arms, because either alone can be satisfied while the image is still broken:
the STATIC arm reads the Dockerfile (it fails on the unfixed file, which is what
makes this test worth having), and the IMAGE arm asks a real container, because
a Dockerfile line that is present but ineffective would pass the static arm.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DOCKERFILE = HERE.parent / "Dockerfile"

_USER_1000 = re.compile(r"^USER\s+1000\s*$", re.M)
_CREATES_1000 = re.compile(r"useradd[^\n]*--uid\s+1000|adduser[^\n]*\b1000\b"
                           r"|>>\s*/etc/passwd", re.M)


def test_the_account_is_created_before_anything_runs_as_it():
    text = DOCKERFILE.read_text()
    runs_as = [m.start() for m in _USER_1000.finditer(text)]
    assert runs_as, "no `USER 1000` in the Dockerfile — has the runtime uid changed?"
    created = [m.start() for m in _CREATES_1000.finditer(text)]
    assert created, (
        "the Dockerfile runs as uid 1000 but never creates it. getpass.getuser() "
        "raises KeyError and `id -un` exits non-zero for a uid with no passwd "
        "entry (#145).")
    assert min(created) < min(runs_as), (
        "uid 1000 is created only AFTER a stage already runs as it — every "
        "command in that earlier stage still sees a nameless uid.")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_a_real_container_can_say_who_it_is():
    image = os.environ.get("VIBEIC_EDA_IMAGE")
    if not image:
        pytest.skip("set VIBEIC_EDA_IMAGE to run the in-image arm")
    probe = (
        'python3 -c "import getpass,pwd,os;'
        'n=getpass.getuser();'
        'assert n==pwd.getpwuid(os.getuid())[0];'
        r'assert chr(10) not in n and n.strip()==n, repr(n);'
        'print(\'NAME=\'+n)"'
    )
    cp = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                         image, "-c", probe],
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, (
        f"the image cannot name its own runtime user:\n{cp.stderr.strip()[-600:]}")
    assert "NAME=" in cp.stdout, cp.stdout
    name = cp.stdout.split("NAME=", 1)[1].strip()
    assert name and not name.isdigit(), (
        f"the runtime user resolved to {name!r} — a bare uid means the passwd "
        "entry is still missing")
