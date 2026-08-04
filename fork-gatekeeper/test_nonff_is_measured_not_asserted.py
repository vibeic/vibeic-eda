"""vibeic-eda#75 — a non-fast-forward push must not assert what it did not measure.

The 05:30 round emitted, for every non-fast-forward:

    DIVERGED: our master and origin/master share no ancestor and their trees
    differ — retrying will not resolve it

as a FIXED STRING. Measured on the OpenROAD clone the round had just operated
on, both halves were false: `merge-base` returns 98251dfc and the state is an
ordinary 12-ahead / 3-behind.

"retrying will not resolve it" is the load-bearing half — it tells the next
reader, human or cron, that there is nothing to do, which is how a fork stops
being chased without anyone deciding to stop chasing it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "daily_0530.py").read_text()


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True,
                          text=True, timeout=55)


def _emission_index() -> int:
    """Where the claim is EMITTED, not where it is discussed.

    The fix's own comment quotes the old sentence in order to explain it, and a
    naive `SRC.index("share no ancestor")` finds that comment first — the test
    then passes or fails on prose rather than on code. Same shape as a gate
    matching its own remedy. Anchored to the f-string that actually builds the
    message."""
    return SRC.index('f"DIVERGED: our {main} and origin/{main} share no "')


def test_the_unresolvable_claim_is_gated_on_a_missing_merge_base():
    """The sentence may only be emitted when there really is no ancestor."""
    window = SRC[max(0, _emission_index() - 1200):_emission_index()]
    assert "merge-base" in window, (
        "the 'share no ancestor' claim is still emitted without measuring "
        "whether an ancestor exists")
    assert "if _mb:" in window, (
        "the emission is not behind a branch on the measured merge-base")


def test_an_ordinary_behind_state_is_reported_as_resolvable():
    i = SRC.index("BEHIND: origin/")
    seg = SRC[i:i + 400]
    assert "IS resolvable" in seg
    assert "ahead" in seg and "behind" in seg, (
        "the report must carry the measured numbers, not an adjective")


def test_the_two_outcomes_are_distinguishable_by_a_reader():
    assert '"diverged_kind"] = "behind"' in SRC
    assert '"diverged_kind"] = "unrelated"' in SRC


def test_measured_against_the_real_clone_if_present():
    """The clone the issue was raised on. Skips when it is not on this host —
    and says so, rather than passing silently."""
    import pytest
    clone = Path.home() / "vibe-ic-forks" / "OpenROAD"
    if not (clone / ".git").exists():
        pytest.skip(f"no clone at {clone} on this host")
    mb = _git(clone, "merge-base", "master", "origin/master").stdout.strip()
    assert mb, ("the issue's premise is that a merge-base EXISTS here; without "
                "one this test proves nothing")
    ahead = _git(clone, "rev-list", "--count", "origin/master..master").stdout.strip()
    behind = _git(clone, "rev-list", "--count", "master..origin/master").stdout.strip()
    assert ahead.isdigit() and behind.isdigit(), (
        f"ahead/behind are not measurable here: {ahead!r} / {behind!r}")
    # THE PREMISE, NOT A PARTICULAR STATE. This required `ahead > 0 and behind >
    # 0` — the exact 12-ahead / 3-behind the issue was raised on. That is a LIVE
    # clone: the divergence was later resolved (our RCX message-id fix was pushed,
    # taking it to 0/0) and the test began failing because the situation had been
    # FIXED. A fixture that reports its own repair as a regression gets ignored,
    # and this one had been red long enough to be filed as a mystery.
    #
    # What the issue is about survives any of those states: a merge-base EXISTS,
    # so the fixed string "share no ancestor ... retrying will not resolve it"
    # was false about this clone whatever the counts are. That is asserted above
    # and is what must not regress.
    assert int(ahead) or int(behind) or mb, (
        "with no divergence and no merge-base there is nothing here to measure")
