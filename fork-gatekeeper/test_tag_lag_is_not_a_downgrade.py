"""A release tag trailing the branch we track is not a downgrade.

WHY THIS EXISTS (measured 2026-08-06)
=====================================
`target_direction` refuses a target that is not a descendant of the ref we ship.
That is right for Trilinos, which is what put it there: the report proposed
`→ trilinos-release-17-1-1` on three consecutive days while our pin sat 407
commits AHEAD of that tag.

But applied alone, the predicate called **20 of 29** tools BEHIND:

    OpenSTA   pin 2273 ahead of v2.2.0
    OpenROAD  pin 1140 ahead of 26Q3
    cocotb    pin  544 ahead of v2.0.1
    ...

Most of our forks follow upstream's DEFAULT BRANCH, and a release tag trails the
branch by construction — so "our pin is ahead of the newest tag" is the NORMAL
state, not a fault. Refusing all twenty would have made the daily report silent
about two thirds of the fleet. **A guard that fires on the normal case is not a
guard; it is an outage.**

THE QUESTION THAT ACTUALLY SEPARATES THEM
-----------------------------------------
Is the tag on the line we track?

    OpenROAD  tag IS an ancestor of upstream/master  -> tag-lag. We are ahead
                                                        because we follow master.
                                                        Nothing would be lost.
    cocotb    tag is NOT, and we are 0 behind master -> the tag is a side branch
                                                        we have already passed.
    Trilinos  tag is NOT, and we are 42 behind       -> the same, plus a real gap
                                                        on the branch — which the
                                                        commit-level question
                                                        reports separately.

With that added: 20 BEHIND -> **6 BEHIND, 14 tag-lag-only**.

WHAT THIS FILE ASSERTS
----------------------
The distinction itself, on constructed repos, so it cannot pass by accident on a
fleet that happens to be arranged conveniently today. The fleet-shaped assertion
lives at the bottom and is skipped when the clones are absent.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import assess_release as A  # noqa: E402


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), *a],
                          capture_output=True, text=True).stdout.strip()


def _repo(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    return d


def _commit(d: Path, msg: str) -> str:
    (d / "f.txt").write_text(msg)
    _git(d, "add", "f.txt")
    _git(d, "commit", "-qm", msg)
    return _git(d, "rev-parse", "HEAD")


def _direction(tool, our_ref, fork_point, target, up_branch):
    """Call `target_direction` in a way BOTH the pre-fix and post-fix signatures
    accept, so a failure here is BEHAVIOURAL.

    The first draft of this file passed `up_branch` positionally. Against pristine
    `origin/main` every test then died with `TypeError: takes 4 positional
    arguments but 5 were given` — which proves only that I added a parameter, not
    that the behaviour was wrong. A test that dies on a signature change is not a
    red proof; this repo's own standard says so, and the first draft failed it.
    """
    import inspect
    try:
        params = inspect.signature(A.target_direction).parameters
    except (TypeError, ValueError):                                  # pragma: no cover
        params = {}
    if len(params) >= 5:
        return A.target_direction(tool, our_ref, fork_point, target, up_branch)
    return A.target_direction(tool, our_ref, fork_point, target)


@pytest.fixture()
def fork(tmp_path, monkeypatch):
    """A clone shaped like ours: a `master` we follow, and tags on and off it."""
    root = tmp_path / "forks"
    d = _repo(root, "tool")
    base = _commit(d, "base")
    _git(d, "tag", "v1.0")                       # tag ON master, behind the tip
    _commit(d, "upstream work 1")
    _commit(d, "upstream work 2")
    tip = _git(d, "rev-parse", "HEAD")
    _git(d, "branch", "-f", "master", tip)
    # a tag on a SIDE branch, not reachable from master
    _git(d, "checkout", "-q", "-b", "side", base)
    _commit(d, "side work")
    _git(d, "tag", "v2.0-side")
    _git(d, "checkout", "-q", "master")
    monkeypatch.setattr(A, "FORKS_DIR", root)
    return {"dir": d, "base": base, "tip": tip}


def test_a_tag_on_the_tracked_branch_is_not_a_downgrade(fork):
    """THE REGRESSION. Our pin is ahead of `v1.0` because we follow master — the
    ordinary state of most of the fleet, and it must not be refused."""
    r = _direction("tool", fork["tip"], None, "v1.0", "master")
    assert r["verdict"] == A.FORWARD, (
        f"a tag that IS an ancestor of the branch we track was called "
        f"{r['verdict']!r}. Measured on the real fleet, this predicate alone "
        f"refused 20 of 29 tools, including OpenROAD (pin 1140 ahead of 26Q3). "
        f"why={r.get('why')!r}")
    assert r.get("tag_lag_only") is True, (
        "the row does not record that this is tag-lag rather than a genuine "
        "forward target; a reader cannot tell 'nothing to do' from 'ready to "
        "advance'")


def test_a_tag_off_the_tracked_branch_is_still_refused(fork):
    """THE BIDIRECTIONAL CONTROL. Softening the rule must not soften it into
    nothing — a guard that accepts everything passes the test above."""
    r = _direction("tool", fork["tip"], None, "v2.0-side", "master")
    assert r["verdict"] == A.BEHIND, (
        f"a tag on a SIDE branch we have already passed was called "
        f"{r['verdict']!r}; adopting it would drop work we build. "
        f"why={r.get('why')!r}")
    assert not r.get("tag_lag_only")


def test_an_unmeasurable_branch_is_not_silently_forward(fork):
    """THE THIRD STATE. If we cannot resolve the branch we track, we cannot tell
    tag-lag from a downgrade — and that must not read as either."""
    r = _direction("tool", fork["tip"], None, "v2.0-side",
                           "no-such-branch-deadbeef")
    assert r["verdict"] != A.FORWARD, (
        f"an unresolvable tracked branch produced {r['verdict']!r}; 'we could "
        f"not check' must never render as 'checked and fine'")


def test_a_genuinely_forward_target_still_passes(fork):
    """Control: the case the whole mechanism exists to allow."""
    r = _direction("tool", fork["base"], None, "master", "master")
    assert r["verdict"] == A.FORWARD
    assert not r.get("tag_lag_only"), (
        "a real forward target was mislabelled as tag-lag, which would tell a "
        "reader there is nothing to do when there is")


@pytest.mark.parametrize("tool,expected", [
    ("OpenROAD", "tag_lag"),   # pin 1140 ahead, tag ON master
    ("cocotb", A.BEHIND),      # pin 544 ahead, tag OFF master
    ("Trilinos", A.BEHIND),    # pin 407 ahead, tag OFF master — the original case
])
def test_the_real_fleet_agrees(tool, expected):
    """The fleet-shaped assertion, skipped when the clones are not present.

    Named tools rather than a count: a count moves every time a pin moves, and a
    test that has to be edited on every release is one people edit without
    reading.
    """
    led = Path.home() / ".cache/eda-fork-gatekeeper/ledger" / f"{tool}.json"
    if not led.is_file() or not (A.FORKS_DIR / tool / ".git").is_dir():
        pytest.skip(f"{tool}: no ledger or no clone on this host")
    d = json.loads(led.read_text())
    r = _direction(tool, d.get("pinned_ref_full") or d.get("pinned_ref"),
        (d.get("fork_point") or {}).get("sha"),
        d.get("upstream_latest_release"),
        d.get("upstream_default_branch") or "master")
    if expected == "tag_lag":
        assert r["verdict"] == A.FORWARD and r.get("tag_lag_only"), (
            f"{tool}: expected ordinary tag-lag, got {r['verdict']!r} "
            f"tag_lag_only={r.get('tag_lag_only')!r} — why={r.get('why')!r}")
    else:
        assert r["verdict"] == expected, (
            f"{tool}: expected {expected!r}, got {r['verdict']!r} — "
            f"why={r.get('why')!r}")
