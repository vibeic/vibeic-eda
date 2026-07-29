#!/usr/bin/env python3
"""Two guards that were red or silent on main, and neither told anyone.

vibeic-eda#27. Both defects were found by RUNNING the guards during an audit,
which is the reason these tests exist: a guard nobody exercises is a guard that
reports whatever it last happened to report.

  * `check_fork_only` was RED on main. `ALLOWED_BASE` ended in `(:.*)?`, which
    matches `name:tag` and does NOT match `name@sha256:…`. Pinning the base by
    digest is stricter than a moving tag, and the check punished the repository
    for getting stricter — the kind of guard that gets switched off.
  * `inventory.py` raised NameError on every call, and `build_page.build()`
    wraps it in a try/except that renders a "measurement failed" block and
    publishes anyway. A crash would have been louder.
"""
from __future__ import annotations

import importlib.util as _u
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = _u.spec_from_file_location(name, path)
    mod = _u.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:                      # a __main__ guard that argparses
        pass
    return mod


@pytest.mark.parametrize("ref,allowed,why", [
    ("hpretl/iic-osic-tools@sha256:abc", True, "the digest-pinned base itself"),
    ("hpretl/iic-osic-tools:latest", True, "the old tag form must still pass"),
    ("ubuntu:24.04", True, "a plain OS base"),
    ("ubuntu@sha256:abc", True, "an OS base by digest"),
    ("scratch", True, "no suffix at all"),
    # The five that make rc=0 mean 'allowed' rather than 'nothing examined'
    ("evil/thing@sha256:abc", False, "a digest does not make a base allowed"),
    ("ghcr.io/evil/ubuntu", False, "a registry-qualified impostor"),
    ("ubuntufoo", False, "an allowed name must not leak into a longer one"),
    ("hpretl/iic-osic-tools-evil", False, "…nor by suffix"),
])
def test_allowed_base_accepts_digests_without_widening(ref, allowed, why):
    m = _load(_ROOT / "tools" / "check_fork_only.py", "cfo")
    assert bool(m.ALLOWED_BASE.match(ref)) is allowed, why


def test_inventory_resolves_the_repo_root_from_its_own_location():
    """The NameError was `ROOT` in a module that defines `DIR`.

    Asserting the module merely imports would not have caught it — the name is
    read inside `collect()`. So this checks the resolution is right AND that the
    path it computes is the repo root, which is what makes the Dockerfile
    readable from there.
    """
    m = _load(_ROOT / "fork-gatekeeper" / "inventory.py", "inv")
    assert not hasattr(m, "ROOT"), \
        "the module defines DIR; a stray ROOT is the defect coming back"
    assert m.DIR.name == "fork-gatekeeper"
    assert (m.DIR.parent / "Dockerfile").is_file(), \
        "DIR.parent must be the repo root, or collect() reads nothing"


def test_inventory_source_has_no_undefined_global():
    """Cheap, and it is the check that would have caught this before a run.

    `collect()` needs a container to execute, so the defect lived in a line no
    test reached. Compiling the module and comparing its global loads against
    what it defines finds an undefined name without running anything.

    NARROWER THAN IT LOOKS, and kept deliberately. This reads one module and only
    ALL-CAPS names, so it is a check for the shape of THIS defect, not for the
    class. Measured: revert the fix and it fails; rename the same defect to a
    lowercase `repo_root`, or put an undefined constant in `build_page.py`, and it
    passes both times. `test_no_undefined_globals.py` is the general form — proper
    scope analysis via `symtable`, every name, all 28 modules — and it catches all
    three. That one is the guard; this one stays as the named regression for #27.
    """
    import ast
    src = (_ROOT / "fork-gatekeeper" / "inventory.py").read_text()
    tree = ast.parse(src)
    defined = {n.id for node in ast.walk(tree)
               if isinstance(node, ast.Assign)
               for n in node.targets if isinstance(n, ast.Name)}
    defined |= {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    defined |= {a.asname or a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names}
    defined |= set(dir(__builtins__)) | set(vars(__builtins__)) \
        if isinstance(__builtins__, dict) is False else set(__builtins__)
    # Only ALL-CAPS module constants: the shape of the bug, without needing a
    # full scope analysis to avoid false positives on locals and comprehensions.
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id.isupper() and len(n.id) > 2}
    missing = sorted(used - defined)
    assert not missing, f"module-level constants used but never defined: {missing}"
