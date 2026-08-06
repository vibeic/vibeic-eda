#!/usr/bin/env python3
"""`published_tip` compared a candidate branch against the repo DEFAULT.
That is wrong for a fork with two independently-maintained lines.

WHY THIS EXISTS (measured 2026-08-07, vibeic-eda#102)
=======================================================
Trilinos has two branches, both legitimately maintained:

    master                                       tracks raw upstream
    vibeic/xyce-trilinos-17.2-epetra-restored     the line we SHIP -- reverts a
                                                   series of upstream commits
                                                   that removed the Epetra stack
                                                   Xyce needs

`published_tip` accepted a ledger-recorded branch only when it contained
`origin/master` (the "default"). The moment `master` picked up even one commit
the shipping branch does not have -- an ORDINARY event on a line we do not even
ship from -- the check rejected the shipping branch and held the whole round at
rc=2, for a fork that had actually converged perfectly (the shipping branch's
tip WAS the current pin).

THE FIX
-------
Compare against the PIN -- "what this fork last shipped" -- when one is known,
falling back to the default only for an unintegrated fork with no pin at all.
The pin is unambiguous regardless of how many lines the repo maintains; the
default branch is not.

WHY THESE TESTS FAIL BEHAVIOURALLY AGAINST THE OLD TREE
=========================================================
Every test drives `published_tip` directly over REAL git repositories and
asserts on the RETURNED VERDICT. None names `basis`, `basis_label`, or any
other symbol private to the fix -- the old tree's `published_tip(clone, led)`
two-argument signature still exists (`pin` was added with a default), so
dropping this file onto the pristine tree calls the OLD comparison (against
default) and gets the OLD, wrong answer: TIP_BEHIND on a branch whose only
"problem" is that an unrelated line moved.

BIDIRECTIONAL. `test_a_genuinely_stale_branch_is_still_rejected` is the control
that matters: a fix that always returns TIP_CURRENT would pass the regression
test trivially and take the #92 protection out entirely. It must still reject a
branch that does NOT contain the pin.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "fgr_pin_basis_under_test", HERE / "fork_gap_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fgr_pin_basis_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tip(m, clone, led, pin):
    """Call `published_tip` in a way BOTH the pristine and fixed signature
    accept, so a failure here is BEHAVIOURAL.

    The pristine tree's `published_tip(clone, led)` has no third parameter at
    all -- calling it with `pin=...` dies with TypeError, which would prove
    only that a parameter was added, not that the old comparison is wrong.
    This repo's own standard says a test that dies on a signature change is
    not a red proof.
    """
    import inspect
    try:
        params = inspect.signature(m.published_tip).parameters
    except (TypeError, ValueError):                                  # pragma: no cover
        params = {}
    if len(params) >= 3:
        return m.published_tip(clone, led, pin)
    return m.published_tip(clone, led)


def _git(cwd, *a, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *a],
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {a} failed: {r.stderr}")
    return r.stdout.strip()


def _commit(repo, name, msg):
    (repo / name).write_text(msg)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def two_line_fork(tmp_path):
    """A fork shaped like Trilinos: `master` tracks upstream on its own; a
    SEPARATE branch is the one we actually ship, and has its own history.

    Topology:

        origin/master     base - m1              (moved on its own, unrelated)
        origin/vibeic/ship base - s1 - s2         (the shipping line; s2 == PIN)
    """
    up = tmp_path / "upstream"; up.mkdir()
    _git(up, "init", "-q", "-b", "master")
    base = _commit(up, "a.txt", "base")

    ours = tmp_path / "ourfork.git"
    _git(tmp_path, "clone", "-q", "--bare", str(up), str(ours))

    fork = tmp_path / "forks" / "toolx"; fork.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(ours), str(fork))

    # the shipping line: two commits, diverging from master immediately
    _git(fork, "checkout", "-q", "-b", "vibeic/ship")
    s1 = _commit(fork, "b.txt", "ship 1")
    s2 = _commit(fork, "c.txt", "ship 2")
    _git(fork, "push", "-q", "origin", "vibeic/ship")

    # master moves separately, on its own, after the shipping line branched off
    _git(fork, "checkout", "-q", "master")
    m1 = _commit(fork, "d.txt", "master-only work")
    _git(fork, "push", "-q", "origin", "master")

    _git(fork, "fetch", "-q", "origin")

    return {"fork": fork, "forks_root": fork.parent, "base": base,
            "s1": s1, "s2": s2, "m1": m1}


def test_a_shipping_branch_is_accepted_even_though_an_unrelated_line_moved(
        two_line_fork):
    """THE REGRESSION. `vibeic/ship`'s tip IS the pin -- it must be TIP_CURRENT
    regardless of what `master` (a different line) did in the meantime."""
    m = _load()
    led = {"tool": "toolx", "vibeic_branch": "vibeic/ship"}
    v = _tip(m, two_line_fork["fork"], led, two_line_fork["s2"])
    assert v.state == m.TIP_CURRENT, (
        f"a shipping branch whose tip equals the pin was rejected because an "
        f"UNRELATED line (master) moved -- this is the #102 defect. "
        f"state={v.state!r} why={v.why!r}")
    assert v.ref == f"origin/vibeic/ship"


def test_a_genuinely_stale_branch_is_still_rejected(two_line_fork):
    """BIDIRECTIONAL CONTROL. A branch that does NOT contain the pin must still
    be rejected -- the fix must not have removed the #92 protection."""
    m = _load()
    _git(two_line_fork["fork"], "push", "-q", "origin",
        f"{two_line_fork['base']}:refs/heads/vibeic/stale")
    _git(two_line_fork["fork"], "fetch", "-q", "origin")
    led = {"tool": "toolx", "vibeic_branch": "vibeic/stale"}
    v = _tip(m, two_line_fork["fork"], led, two_line_fork["s2"])
    assert v.state == m.TIP_BEHIND, (
        f"a branch that does not contain the pin was accepted -- the fix has "
        f"gutted the #92 protection. state={v.state!r} why={v.why!r}")


def test_a_branch_that_has_advanced_past_the_pin_is_still_accepted(
        two_line_fork):
    """A shipping branch AHEAD of the pin (new work not yet released) must
    still be TIP_CURRENT -- release_lag, not a rejection."""
    m = _load()
    s3 = _commit(two_line_fork["fork"], "e.txt", "ship 3, not yet released")
    _git(two_line_fork["fork"], "push", "-q", "origin", "vibeic/ship")
    led = {"tool": "toolx", "vibeic_branch": "vibeic/ship"}
    v = _tip(m, two_line_fork["fork"], led, two_line_fork["s2"])
    assert v.state == m.TIP_CURRENT, (
        f"a branch ahead of the pin (ordinary unreleased work) was rejected. "
        f"state={v.state!r} why={v.why!r}")


def test_no_pin_falls_back_to_the_default_as_before(two_line_fork):
    """An unintegrated fork with no pin yet has no sharper answer available --
    must fall back to the default branch, matching the pre-#102 behaviour."""
    m = _load()
    led = {"tool": "toolx", "vibeic_branch": "vibeic/ship"}
    v = _tip(m, two_line_fork["fork"], led, None)
    # vibeic/ship (s1,s2) does not contain master's m1 -- rejected against
    # the default, exactly as the old code would have done.
    assert v.state == m.TIP_BEHIND, (
        f"with no pin available, the fallback to the default branch did not "
        f"fire. state={v.state!r} why={v.why!r}")


def test_a_pin_that_does_not_resolve_here_degrades_to_the_default(
        two_line_fork):
    """A pin sha the CLONE has never seen (wrong host, not yet fetched) must
    not crash the comparison -- it degrades to the default, exactly like a
    missing pin."""
    m = _load()
    led = {"tool": "toolx", "vibeic_branch": "vibeic/ship"}
    bogus_pin = "f" * 40
    v = _tip(m, two_line_fork["fork"], led, bogus_pin)
    assert v.state == m.TIP_BEHIND, (
        f"an unresolvable pin was not handled -- expected a graceful fallback "
        f"to the default branch. state={v.state!r} why={v.why!r}")
