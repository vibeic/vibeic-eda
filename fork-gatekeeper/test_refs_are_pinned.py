"""The pin guard must SEE every ref, not just the ones its regex happens to fit.

This test exists because the first version of `check_refs_are_pinned.py` did
not. Its pattern ended `(\\S+)\\s*$`, requiring the value to be the last thing on
the line, and nearly every pinned ref in this repo carries a trailing comment:

    ARG OPENROAD_REF=cf27a31e509d...  # pinned; branch master (0.2.22 ...)
    ARG KLAYOUT_REF=e89f9b272eb4...   # pinned; branch vibeic/klayout-signoff-int

So it matched 14 of the 35 refs present, missed OPENROAD, KLAYOUT, MAGIC,
NETGEN, NGSPICE, SBY, TRILINOS, VERILATOR and thirteen more, and printed

    check_refs_are_pinned: PASS — 14 ref(s) across 20 Dockerfile(s)

A checker with a silent blind spot exactly the shape of the defect it exists to
find is worse than no checker: it converts "nobody looked" into "somebody
looked and it was fine". The bug was found only because the number disagreed
with a grep run for an unrelated reason.

So the count is pinned against an INDEPENDENT measurement -- a plain grep with
a different pattern, in this file, that knows nothing about the checker's
regex. If the two ever disagree, one of them is blind, and the test says so
rather than either of them reporting a confident PASS.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CHECKER = ROOT / "tools" / "check_refs_are_pinned.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_refs_are_pinned", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_refs_are_pinned"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()

#: Deliberately NOT the checker's pattern: no anchoring on the end of the line,
#: no comment handling, no `_REF` suffix subtlety beyond the name itself.
INDEPENDENT = re.compile(r"^[ \t]*ARG[ \t]+([A-Z0-9_]+_REF)[ \t]*=", re.M)


def _independent_names(root: pathlib.Path) -> list[str]:
    names = []
    for f in M.dockerfiles(root):
        names += INDEPENDENT.findall(f.read_text(encoding="utf-8", errors="replace"))
    return sorted(names)


def _checker_names(root: pathlib.Path) -> list[str]:
    names = []
    for f in M.dockerfiles(root):
        text = f.read_text(encoding="utf-8", errors="replace")
        names += [m.group(1) for m in M.ARG_REF.finditer(text)]
    return sorted(names)


def test_the_guard_sees_every_ref_an_independent_pattern_sees():
    """The regression. Two patterns, written apart, must agree on the set."""
    theirs = _independent_names(ROOT)
    ours = _checker_names(ROOT)
    assert theirs, "the independent scan found no ARG *_REF at all — it is broken"
    missing = sorted(set(theirs) - set(ours))
    extra = sorted(set(ours) - set(theirs))
    assert not missing, (
        f"check_refs_are_pinned cannot see {len(missing)} ref(s) that a plain "
        f"grep finds: {missing}. It would report PASS while never having looked "
        f"at them.")
    assert not extra, f"the checker invented refs the tree does not have: {extra}"


def test_a_trailing_comment_does_not_hide_a_ref(tmp_path):
    """The exact shape that caused the blind spot, in isolation."""
    (tmp_path / "Dockerfile").write_text(
        "FROM x\n"
        "ARG BARE_REF=" + "a" * 40 + "\n"
        "ARG COMMENTED_REF=" + "b" * 40 + "  # pinned; branch master (notes)\n"
        "ARG TABBED_REF=" + "c" * 40 + "\t# tab before the comment\n")
    names = _checker_names(tmp_path)
    assert names == ["BARE_REF", "COMMENTED_REF", "TABBED_REF"], names


def test_a_branch_behind_a_trailing_comment_is_still_caught(tmp_path):
    """The obverse: seeing it is not enough, it must still be judged."""
    (tmp_path / "Dockerfile").write_text(
        "FROM x\nARG THING_REF=main  # we always take the tip\n")
    findings, pinned = M.scan(tmp_path)
    assert pinned == 0
    assert [(f[2], f[3]) for f in findings] == [("THING_REF", "main")]


def test_an_indirection_is_followed_not_called_a_branch(tmp_path):
    """`ARG A_REF=${B}` where B is a SHA is pinned, and must not be a finding."""
    (tmp_path / "Dockerfile").write_text(
        "FROM x\n"
        "ARG BASE=" + "d" * 40 + "\n"
        "ARG DERIVED_REF=${BASE}\n")
    findings, pinned = M.scan(tmp_path)
    assert findings == [] and pinned == 1


def test_no_dockerfile_is_could_not_check_not_pass(tmp_path):
    """Absence is never a pass."""
    rc = M.main(["--root", str(tmp_path)])
    assert rc == 2


def test_the_real_tree_passes_as_a_program():
    """Run it the way the tick does."""
    r = subprocess.run([sys.executable, str(CHECKER), "--root", str(ROOT)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "every one a commit" in r.stdout
