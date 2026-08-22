"""The anchor was removed and only one of its two consumers was told.

`daily_release` used to move vibe-ic's pinned image version and record an
`anchor_pr`. The pin claimed to be "what the plugin was verified against" and
nothing ever ran that verification, so the anchor was removed (vibeic-eda
8ed5f33). `capture_release_ll`'s PUBLISHED branch was taught that absence is now
the expected shape. Its NOOP branch was not, and kept demanding evidence nothing
writes any more.

That survived because it needs a NOOP release to reach it, and every round after
the removal published a new version. 2026-08-22 14:28 was the first noop:
`run_tick exit 0`, nothing to rebuild, and the LL rejected the round with
"release noop has no successful anchor verification" — which then skipped the
page.

Half a fix is how a consumer outlives its producer in the one branch nobody
exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SOURCE = (HERE / "capture_release_ll.py").read_text(encoding="utf-8")


def _noop_branch() -> str:
    i = SOURCE.index('if status == "noop":')
    return SOURCE[i:SOURCE.index("    else:", i)]


def _published_branch() -> str:
    i = SOURCE.index("# NO ANCHOR TO PROVE.")
    return SOURCE[i:i + 1200]


def test_a_noop_release_without_an_anchor_is_not_an_error():
    """THE REGRESSION. Nothing writes `anchor_pr`, so requiring it fails every
    noop release for ever."""
    branch = _noop_branch()
    assert "release noop has no successful anchor verification" not in branch
    assert "if anchor is not None:" in branch, (
        "absence must be the expected shape, as it already is when published")


def test_a_noop_release_WITH_a_failed_anchor_is_still_an_error():
    """THE DIRECTION THAT KEEPS IT HONEST. Absence is expected; a present but
    failed anchor means something re-wired that path without updating this
    file, and saying nothing about it is how a stale consumer becomes a silent
    one — the published branch's own words."""
    assert "release noop carries a failed anchor verification" in _noop_branch()


def test_both_branches_agree_about_what_absence_means():
    """The two branches describe ONE fact: the anchor no longer exists. They
    disagreed for as long as no round was a noop."""
    for branch in (_noop_branch(), _published_branch()):
        assert "if anchor is not None:" in branch


def test_nothing_still_produces_an_anchor():
    """If the anchor ever comes back, these tests are the wrong shape and should
    fail rather than quietly permit a half-wired return."""
    producer = (HERE / "daily_release.py").read_text(encoding="utf-8")
    assert '"anchor_pr"' not in producer, (
        "daily_release writes an anchor again; the LL's rules need revisiting")
