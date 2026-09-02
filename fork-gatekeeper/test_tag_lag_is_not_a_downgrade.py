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
    side = _commit(d, "side work")
    _git(d, "tag", "v2.0-side")
    _git(d, "checkout", "-q", "master")
    monkeypatch.setattr(A, "FORKS_DIR", root)
    return {"dir": d, "base": base, "tip": tip, "side": side}


def test_a_tag_on_the_tracked_branch_is_not_a_downgrade(fork):
    """THE REGRESSION. Our pin is ahead of `v1.0` because we follow master — the
    ordinary state of most of the fleet, and it must not be refused."""
    r = _direction("tool", fork["tip"], None, "v1.0", "master")
    assert r.get("why"), "a verdict with no why-line is not reportable"
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
    assert r.get("why"), "a verdict with no why-line is not reportable"
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


def test_a_target_containing_our_fork_point_is_forward(fork):
    """THE SECOND FORWARD BRANCH, and it had no test in this file.

    Found by mutating the predicate on 2026-09-02: replacing the fork-point
    branch with `if False` left every test here green, because no fleet row
    happens to sit in that shape today. The branch is what makes `netgen 1.5.323`
    a real target while Trilinos is not — a target that does not descend from our
    pin but does contain the point we forked from lacks only our own carried
    patches, which a merge re-applies.
    """
    r = _direction("tool", fork["side"], fork["base"], "master", "master")
    assert r.get("why"), "a verdict with no why-line is not reportable"
    assert r["verdict"] == A.FORWARD, (
        f"a target containing our fork point was called {r['verdict']!r}; the "
        f"only commits it lacks are our own. why={r.get('why')!r}")
    assert not r.get("tag_lag_only"), (
        "a genuine forward target was labelled tag-lag, which tells a reader "
        "there is nothing to do when there is")


def test_a_failed_ancestry_probe_is_not_read_as_not_an_ancestor(fork):
    """rc 0 and rc 1 are the only two answers `--is-ancestor` gives. Anything
    else is the PROBE failing, and folding that into False turns "git could not
    tell us" into the substantive verdict BEHIND -- a downgrade refusal invented
    out of an unavailable repository. The clone is fine here; only the probe is
    made to fail, so nothing else about the world changes."""
    real = A._git_in

    def probe_fails(clone, *args, **kw):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
        return real(clone, *args, **kw)

    A._git_in = probe_fails
    try:
        r = _direction("tool", fork["tip"], None, "v1.0", "master")
    finally:
        A._git_in = real
    assert r["verdict"] == A.DIR_UNKNOWN, (
        f"a probe that did not answer produced the substantive verdict "
        f"{r['verdict']!r}; 'we could not check' is its own state. "
        f"why={r.get('why')!r}")
    assert "did not answer" in (r.get("why") or ""), (
        f"the why-line does not say the probe failed: {r.get('why')!r}")


def test_a_genuinely_forward_target_still_passes(fork):
    """Control: the case the whole mechanism exists to allow."""
    r = _direction("tool", fork["base"], None, "master", "master")
    assert r["verdict"] == A.FORWARD
    assert r.get("why"), (
        "the pin-ancestor branch produced a verdict with no why-line; the row "
        "would render on the page and in the ledger with nothing to read")
    assert not r.get("tag_lag_only"), (
        "a real forward target was mislabelled as tag-lag, which would tell a "
        "reader there is nothing to do when there is")


#: The fleet rows this file measures. NAMES ONLY — what each one's verdict
#: SHOULD be is derived below from git, never typed here. See
#: `test_the_real_fleet_agrees`.
FLEET_ROWS = ("OpenROAD", "cocotb", "Trilinos")


def _is_ancestor(clone: Path, a: str, b: str) -> bool | None:
    """Raw ancestry, asked of git directly rather than through `assess_release`.

    The point of the fleet test is that the predicate agrees with the repository.
    Deriving the expectation through the predicate's own helpers would make it
    agree with itself, which is not a measurement.
    """
    r = subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", a, b],
                       capture_output=True, text=True)
    return r.returncode == 0 if r.returncode in (0, 1) else None


def _expected_row(clone: Path, pin: str, fork_point: str | None,
                  target: str, up_branch: str):
    """What the four-branch predicate must answer, computed from git alone.

    Mirrors `assess_release.target_direction`'s documented order — pin-ancestor,
    then fork-point-contained, then tag-on-the-tracked-line, else BEHIND — using
    nothing but `merge-base --is-ancestor` on the clone.
    """
    def resolve(ref):
        r = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify",
                            "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    pin_sha, tgt_sha = resolve(pin), resolve(target)
    if not pin_sha or not tgt_sha:
        return None                                   # unmeasurable: nothing to assert
    if _is_ancestor(clone, pin_sha, tgt_sha):
        return (A.FORWARD, False)
    if fork_point:
        fp_sha = resolve(fork_point)
        if fp_sha and _is_ancestor(clone, fp_sha, tgt_sha):
            return (A.FORWARD, False)
        if not fp_sha:
            return None
    up_sha = resolve(up_branch) or resolve(f"upstream/{up_branch}")
    if up_sha and _is_ancestor(clone, tgt_sha, up_sha):
        return (A.FORWARD, True)
    if up_sha is None:
        return None
    return (A.BEHIND, False)


@pytest.mark.parametrize("tool", FLEET_ROWS)
def test_the_real_fleet_agrees(tool):
    """The fleet-shaped assertion, skipped when the clones are not present.

    THE EXPECTATION IS DERIVED, NOT TYPED (measured 2026-09-02)
    ----------------------------------------------------------
    This test used to carry a literal verdict per tool. Those literals are a bet
    on today's upstream, and the pipeline moves the world itself: on 2026-08-31
    the cocotb row was changed BEHIND -> FORWARD at 07:06 against image 0.3.40's
    pin, and at 08:41 the same morning's release moved `COCOTB_REF` to a merge
    whose fork point sits AFTER the v2.1.0 branch point. The tag stopped
    containing the fork point, the predicate correctly said BEHIND again, and the
    literal was wrong 95 minutes after it was written. It went red on the next
    round and stayed red, and because a red suite gates the release half, the
    image and https://vibeic.ai/eda-forks.html both stopped moving.

    A release moving a pin is the NORMAL operation of this pipeline. A test that
    a normal operation turns red is not a guard; the docstring above already says
    so about the predicate, and it is just as true of the test.

    So the row names a tool and nothing else. What its verdict must be is
    computed from `merge-base --is-ancestor` on the clone, asked of git directly
    rather than through `assess_release`, and the assertion is that the predicate
    agrees with the repository. The four constructed-repo tests above remain the
    proof that the DISTINCTION itself is right; this one proves it still holds on
    live data, whatever the live data happens to be today.
    """
    led = Path.home() / ".cache/eda-fork-gatekeeper/ledger" / f"{tool}.json"
    clone = A.FORKS_DIR / tool
    if not led.is_file() or not (clone / ".git").is_dir():
        pytest.skip(f"{tool}: no ledger or no clone on this host")
    d = json.loads(led.read_text())
    pin = d.get("pinned_ref_full") or d.get("pinned_ref")
    fork_point = (d.get("fork_point") or {}).get("sha")
    target = d.get("upstream_latest_release")
    up_branch = d.get("upstream_default_branch") or "master"
    if not pin or not target:
        pytest.skip(f"{tool}: ledger records no pin or no release target")

    expected = _expected_row(clone, pin, fork_point, target, up_branch)
    r = _direction(tool, pin, fork_point, target, up_branch)
    if expected is None:
        # git could not answer. The predicate must not answer either — "we could
        # not check" is its own state and must never render as FORWARD.
        assert r["verdict"] != A.FORWARD, (
            f"{tool}: git could not resolve the refs this row compares, yet the "
            f"predicate returned {r['verdict']!r} — why={r.get('why')!r}")
        return
    verdict, tag_lag = expected
    assert (r["verdict"], bool(r.get("tag_lag_only"))) == (verdict, tag_lag), (
        f"{tool}: git says {verdict!r} tag_lag_only={tag_lag!r} for pin "
        f"{pin[:12]} / fork point {(fork_point or '-')[:12]} / target {target} "
        f"on {up_branch}, but the predicate returned {r['verdict']!r} "
        f"tag_lag_only={r.get('tag_lag_only')!r} — why={r.get('why')!r}")
    assert r.get("why"), f"{tool}: a verdict with no why-line is not reportable"


def test_an_unresolvable_ref_is_not_asserted_as_agreement(fork):
    """THE QUIET BRANCH, and it is unreachable on this host so nothing exercised
    it. When git cannot resolve a row's refs, `_expected_row` returns None and
    the fleet assertion collapses to "the predicate did not say FORWARD". That
    collapse has to be proved, not assumed: a rubber-stamp oracle that returned
    None for everything would silently switch the whole fleet row off."""
    d = fork["dir"]
    assert _expected_row(d, "deadbeefdeadbeef", None, "v1.0", "master") is None
    assert _expected_row(d, fork["tip"], None, "no-such-tag-deadbeef",
                         "master") is None
    assert _expected_row(d, fork["tip"], "deadbeefdeadbeef", "v2.0-side",
                         "master") is None
    # and the predicate must agree that it could not answer -- "we could not
    # check" must never render as "checked and fine".
    r = _direction("tool", fork["tip"], None, "no-such-tag-deadbeef", "master")
    assert r["verdict"] != A.FORWARD and r.get("why")


def test_the_derived_expectation_is_not_a_rubber_stamp(fork):
    """THE CONTROL FOR THE CONTROL. `_expected_row` is the fleet test's oracle;
    if it agreed with everything, the fleet row would assert nothing. On the
    constructed repo it must separate the same three cases the tests above do."""
    d = fork["dir"]
    assert _expected_row(d, fork["tip"], None, "v1.0", "master") == (A.FORWARD, True)
    assert _expected_row(d, fork["tip"], None, "v2.0-side", "master") == (A.BEHIND, False)
    assert _expected_row(d, fork["base"], None, "master", "master") == (A.FORWARD, False)
    # the fork-point branch: our pin is NOT an ancestor of the target, but the
    # target contains the point we forked from, so the only thing it lacks is
    # our own carried work. FORWARD, and not tag-lag.
    assert _expected_row(d, fork["side"], fork["base"], "master",
                         "master") == (A.FORWARD, False)
    assert _expected_row(d, fork["tip"], None, "v2.0-side",
                         "no-such-branch-deadbeef") is None
    # the oracle must not answer None for everything: three of the four cases
    # above returned a verdict, so None means "could not measure", not "off".
