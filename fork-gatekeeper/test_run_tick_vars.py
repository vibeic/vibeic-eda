#!/usr/bin/env python3
"""`run_tick.sh` runs under `set -u`, so a variable assigned only inside a
conditional branch and read outside it is not a skip — it aborts the whole tick.

Hit exactly once: `REL_VER`/`REACH_IMG` were derived inside
`if [ -f fork_reaches_flow_check.py ]` while the capability sweep and the
image-provenance guard both read `${REACH_IMG}` afterwards. A missing checker
would have taken the gatekeeper round down with it:

    $ bash -c 'set -uo pipefail; if false; then X="v"; fi; [ -n "${X}" ]'
    bash: line 1: X: unbound variable

The check is indentation-based, which is a proxy rather than a parse — good
enough for this file's shape and honest about what it is. Validated against the
pre-fix revision, where it flags REACH_IMG.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TICK = Path(__file__).resolve().parent / "run_tick.sh"


def _scan(lines):
    assign, use = {}, {}
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)=", ln)
        if m:
            assign.setdefault(m.group(2), []).append((i, len(m.group(1))))
        for u in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", ln):
            use.setdefault(u.group(1), []).append((i, len(ln) - len(ln.lstrip())))
    bad = []
    for v, uses in use.items():
        a = assign.get(v)
        if not a or not all(ind > 0 for _, ind in a):
            continue
        depth = min(ind for _, ind in a)
        outer = [i for i, ind in uses if ind < depth]
        if outer:
            bad.append((v, a[0][0], outer[0]))
    return bad


def test_no_variable_is_assigned_in_a_branch_and_read_outside_it():
    bad = _scan(TICK.read_text().splitlines())
    assert not bad, "under `set -u` these abort the tick, they do not skip: " + \
        "; ".join(f"${v} assigned line {ai}, read line {ui}" for v, ai, ui in bad)


def test_the_check_catches_the_revision_it_was_written_for():
    """A guard that cannot fail is not a guard — some past revision must trip it.

    Pinned to `HEAD~2` at first, which was true for exactly two commits and then
    failed as a red suite on unrelated work. A test anchored to a MOVING relative
    revision measures how much has landed since, not the thing it names. It now
    walks this file's own history and asserts that at least one past revision
    carried the defect, which stays true however far HEAD moves.
    """
    revs = subprocess.run(
        ["git", "log", "--format=%H", "--", "fork-gatekeeper/run_tick.sh"],
        capture_output=True, text=True, cwd=str(TICK.parent.parent))
    shas = [s for s in revs.stdout.split() if s][:40]
    if not shas:
        import pytest
        pytest.skip("no history for run_tick.sh in this checkout")
    for sha in shas:
        old = subprocess.run(
            ["git", "show", f"{sha}:fork-gatekeeper/run_tick.sh"],
            capture_output=True, text=True, cwd=str(TICK.parent.parent))
        if old.returncode == 0 and any(
                v == "REACH_IMG" for v, _, _ in _scan(old.stdout.splitlines())):
            return
    raise AssertionError(
        "no revision of run_tick.sh in the last %d trips the check — either the "
        "history is shallow or the check stopped working" % len(shas))


def test_the_tick_still_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0
