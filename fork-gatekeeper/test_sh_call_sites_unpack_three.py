"""`_sh` returns three values. Every call site must unpack three.

WHY THIS EXISTS (measured 2026-08-05)
=====================================
`daily_release.py:1033` unpacked two:

    _rc, _dout = _sh(["python3", str(root / "sync_image_version.py"), ...])
    ValueError: too many values to unpack (expected 2)

It crashed the release AFTER the image had composed successfully -- the log
shows `#150 exporting to oci image format`, `#151 importing to docker DONE`,
`#150 DONE 101.9s`, and then the traceback. So the expensive part worked and the
run still cut no version, wrote no RELEASED.json, published nothing, and left
`--json` unwritten.

WHY NOTHING CAUGHT IT
---------------------
The line is inside `if pushed:`. It is reached only on a REAL publish, and there
has not been a successful publish since it was introduced (`99b3f08`; the last
recorded release, 0.2.63, was `6a190fc`). So the defect sat on the one path that
only executes when everything else has already gone right -- which is the path
with the highest cost of failing, and the one a dry run never reaches.

Every other one of the twelve `_sh` call sites in that module unpacks three.
This was a single typo on the least-exercised line.

WHAT THIS ASSERTS
-----------------
Not "line 1033 is correct" -- that is the fix, and a test that only pins the fix
would not have caught the original and will not catch the next one. It asserts
the INVARIANT for every call site, so a new two-value unpack fails here instead
of at 2 a.m. after a 40-minute build.

Deliberately an AST check rather than a regex: `_sh(...)` spans lines at several
call sites, and a regex over source text would have to choose between missing
those and matching things that are not assignments.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
#: Modules that define or call a three-valued `_sh`. Checked by name so a new
#: module that grows one is added here deliberately rather than by a glob that
#: silently starts or stops covering things.
_MODULES = ["daily_release.py", "daily_merge.py", "daily_0530.py"]


def _sh_returns_n(tree: ast.AST) -> int | None:
    """How many values does this module's own `_sh` return? None if it has none."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_sh":
            counts = {
                len(r.value.elts)
                for r in ast.walk(node)
                if isinstance(r, ast.Return)
                and isinstance(r.value, ast.Tuple)
            }
            if not counts:
                return None
            assert len(counts) == 1, (
                f"_sh returns tuples of differing length {sorted(counts)}; a "
                f"caller cannot unpack it correctly at all")
            return counts.pop()
    return None


def _unpack_widths(tree: ast.AST):
    """(lineno, width) for every `a, b = _sh(...)` assignment."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        call = node.value
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_sh"):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Tuple):
            out.append((node.lineno, len(target.elts)))
        else:
            # `x = _sh(...)` binds the whole tuple; legal, not an unpack.
            out.append((node.lineno, None))
    return out


@pytest.mark.parametrize("modname", _MODULES)
def test_every_sh_call_site_unpacks_what_sh_returns(modname):
    path = _DIR / modname
    if not path.is_file():
        pytest.skip(f"{modname} is not present")
    tree = ast.parse(path.read_text())

    n = _sh_returns_n(tree)
    if n is None:
        pytest.skip(f"{modname} defines no _sh of its own")

    wrong = [(ln, w) for ln, w in _unpack_widths(tree)
             if w is not None and w != n]
    assert not wrong, (
        f"{modname}: _sh returns {n} values, but these call sites unpack a "
        f"different number: {wrong}. This raises ValueError at RUNTIME, on "
        f"whatever path reaches that line -- which for daily_release.py:1033 "
        f"was the publish path, so it crashed a release only after the image "
        f"had already composed.")


def test_this_check_can_see_a_bad_call_site():
    """Bidirectional control.

    A checker that walks the AST and finds nothing looks identical to one whose
    matcher is broken. This constructs the defect and requires it to be seen.
    """
    src = (
        "def _sh(cmd):\n"
        "    return 0, '', ''\n"
        "def good():\n"
        "    a, b, c = _sh(['x'])\n"
        "def bad():\n"
        "    a, b = _sh(['x'])\n"
    )
    tree = ast.parse(src)
    assert _sh_returns_n(tree) == 3
    widths = _unpack_widths(tree)
    assert (4, 3) in widths, widths
    assert (6, 2) in widths, widths
    bad = [(ln, w) for ln, w in widths if w is not None and w != 3]
    assert bad == [(6, 2)], bad


def test_a_whole_tuple_binding_is_not_reported():
    """`x = _sh(...)` is legal and must not be flagged, or the check becomes
    noise people learn to ignore."""
    tree = ast.parse("def _sh(c):\n    return 0, '', ''\ndef f():\n    x = _sh(['y'])\n")
    assert _unpack_widths(tree) == [(4, None)]
    assert not [(ln, w) for ln, w in _unpack_widths(tree)
                if w is not None and w != 3]
