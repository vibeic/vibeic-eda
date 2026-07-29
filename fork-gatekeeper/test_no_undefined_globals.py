#!/usr/bin/env python3
"""No function may read a module-level name that does not exist.

WHY THIS EXISTS. `inventory.collect()` shipped reading `ROOT` in three places
while the module defines `DIR`, so every call raised `NameError` and the daily
page rendered "Tool inventory: not rendered — the measurement failed" where its
three tables belong. It was published in that state and nothing went red.

Two things let a whole function be dead on arrival without one test noticing,
and both are still true, which is why this is a test and not a fixed typo:

1. `build_page.build()` wraps the measurement in `except Exception` and turns
   any failure into a caption. That is the right call for a page — a section
   that vanishes looks like one that was never meant to be there — but it means
   the loudest signal the bug can produce is a paragraph nobody diffs.

2. Every test that reaches `build()` replaces the module with a stub
   (`_offline_inventory`), for good reason: the real one costs ~50 GitHub API
   calls and a `docker run`. So the suite can be 187 green with `collect()`
   uncallable, and it was.

A test that called `collect()` for real would fix neither honestly — it would be
slow, need a network and a daemon, and fail for rate limits. This asks the
cheaper question that would still have caught it: does any function read a
global this module never defines? That is answered from the syntax tree, in
milliseconds, offline, and it generalises — it catches the NEXT renamed constant
without anyone adding a case for it.

It is deliberately not a check for the name `ROOT`. Pinning the specific typo
would pass the day someone writes a different one.

`test_guard_regressions.py` carries the named #27 regression for the same defect.
That one reads `inventory.py` alone and only ALL-CAPS names, so it is the record
of what happened; this one is the guard. Measured against three mutations — the
original `ROOT`, the same defect renamed to a lowercase `repo_root`, and an
undefined constant in `build_page.py` — the narrow one catches the first and
misses the other two, and this one catches all three.
"""
from __future__ import annotations

import builtins
import symtable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Names Python binds into every module at import time. `symtable` reports them
#: as global reads because no statement in the file assigns them.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__spec__", "__loader__",
                   "__package__", "__path__", "__builtins__", "__debug__"}


def _sources():
    for d in ("fork-gatekeeper", "tools"):
        for p in sorted((REPO / d).glob("*.py")):
            yield p


def _undefined_globals(path: Path):
    """(scope trail, name) for every global read the module never defines.

    `symtable` resolves scoping properly — parameters, locals, comprehension and
    closure variables are bound, so none of them reach this list. Only a name the
    function reads from module scope, which module scope does not have, does.
    """
    top = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    defined = set(top.get_identifiers()) | _MODULE_DUNDERS
    bad = []

    def walk(table, trail):
        for sym in table.get_symbols():
            name = sym.get_name()
            if (sym.is_global() and not sym.is_assigned()
                    and name not in defined and not hasattr(builtins, name)):
                bad.append((" > ".join(trail), name))
        for child in table.get_children():
            walk(child, trail + [child.get_name()])

    walk(top, [path.name])
    return sorted(set(bad))


@pytest.mark.parametrize("path", list(_sources()), ids=lambda p: p.name)
def test_no_function_reads_a_global_the_module_never_defines(path):
    bad = _undefined_globals(path)
    assert not bad, (
        "%s reads module-level name(s) that do not exist, so the function raises "
        "NameError the first time it runs:\n  %s\nThis is what silenced "
        "inventory.collect(): the module defined DIR and the code said ROOT."
        % (path.relative_to(REPO),
           "\n  ".join("%s -> %s" % (scope, n) for scope, n in bad)))


def test_this_check_can_actually_fail():
    """A guard that cannot fail is not a guard.

    Written against a synthesised module rather than a real one so it keeps
    testing this checker after every real module is clean — the failure mode the
    expired-exception probe in `.github/workflows/fork-only.yml` was rewritten to
    avoid, where a probe silently stops asserting because production data changed
    shape underneath it.
    """
    src = "DIR = 1\n\n\ndef f():\n    return ROOT / 'x'\n"
    tmp = REPO / "fork-gatekeeper" / "_undef_probe_tmp.py"
    tmp.write_text(src, encoding="utf-8")
    try:
        assert _undefined_globals(tmp) == [("_undef_probe_tmp.py > f", "ROOT")]
    finally:
        tmp.unlink()

    # And the corrected form must come back clean, or the check would simply be
    # reporting every module as broken.
    tmp.write_text(src.replace("ROOT", "DIR"), encoding="utf-8")
    try:
        assert _undefined_globals(tmp) == []
    finally:
        tmp.unlink()
