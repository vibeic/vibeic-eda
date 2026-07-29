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
    """A guard that cannot fail is not a guard. The pre-fix tick must trip it."""
    pre = subprocess.run(
        ["git", "show", "HEAD~2:fork-gatekeeper/run_tick.sh"],
        capture_output=True, text=True, cwd=str(TICK.parent.parent))
    if pre.returncode != 0 or not pre.stdout.strip():
        import pytest
        pytest.skip("pre-fix revision not reachable from this checkout")
    assert any(v == "REACH_IMG" for v, _, _ in _scan(pre.stdout.splitlines()))


def test_the_tick_still_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0
