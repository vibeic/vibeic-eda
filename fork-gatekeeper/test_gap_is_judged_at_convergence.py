#!/usr/bin/env python3
"""The publish gate judges the round at the moment it converged.

Measured 2026-08-20. Round 4 merged all 37 forks clean and convergence read
`sync lag 0`. The release phase then took ~45 minutes, YosysHQ pushed two
commits during it, and the gap report — which counts to the LIVE upstream tip —
reported a gap of 2 and skipped the page. Half an hour later the same figure
read 4, without anything having changed on our side.

Requiring the live figure to be zero requires upstream to hold still while we
build. The owner's rule is the convergence point: every number is taken at the
moment the round reached zero. These tests pin that, and pin that it did not
become a way to pass without measuring.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fork_gap_report as G  # noqa: E402


def _rep(**over):
    rep = {
        "q1_image_behind_upstream": 0,
        "q1_image_behind_upstream_at_merge": 0,
        "q2_ours_past_the_pin_substantive": 0,
    }
    rep.update(over)
    return rep


# ── the fix ─────────────────────────────────────────────────────────────────

def test_upstream_moving_during_the_build_does_not_fail_the_round():
    """The exact 2026-08-20 shape: converged clean, upstream moved after."""
    assert G._verdict(_rep(q1_image_behind_upstream=2,
                           q1_image_behind_upstream_at_merge=0)) == 0
    # and it stays 0 however far the live figure drifts
    assert G._verdict(_rep(q1_image_behind_upstream=4,
                           q1_image_behind_upstream_at_merge=0)) == 0
    assert G._verdict(_rep(q1_image_behind_upstream=999,
                           q1_image_behind_upstream_at_merge=0)) == 0


# ── it still has teeth ──────────────────────────────────────────────────────

def test_a_fork_the_round_actually_failed_to_take_still_fails():
    """`tip_seen` is what the round SAW. Still being behind it is a real miss,
    and no amount of live-vs-merge accounting hides that."""
    assert G._verdict(_rep(q1_image_behind_upstream=7,
                           q1_image_behind_upstream_at_merge=7)) == 1
    assert G._verdict(_rep(q1_image_behind_upstream=0,
                           q1_image_behind_upstream_at_merge=1)) == 1


def test_our_own_unshipped_commits_still_fail_the_round():
    assert G._verdict(_rep(q2_ours_past_the_pin_substantive=1)) == 1


# ── it cannot pass on silence ───────────────────────────────────────────────

def test_an_unmeasurable_at_merge_figure_is_NORECORD_not_a_pass():
    """The verdict never sees a report whose at-merge figure is missing:
    `_measurement_incomplete` rejects it first. Without this the new field would be the
    easiest way yet to score a round clean by not measuring it."""
    base = {
        "registered_forks": [], "ledger_forks": [], "merge_tips_seen": {},
        "fleet_errors": [], "q1_unmeasured": [], "q1_unmeasured_at_merge": [],
        "q2_unmeasured_ship": [], "q2_unmeasured_stranded": [],
        "tip_rejected": [], "q1_unmeasured_image_behind_at_merge": [],
    }
    assert G._measurement_incomplete(base) is False
    blind = dict(base, q1_unmeasured_image_behind_at_merge=["yosys"])
    assert G._measurement_incomplete(blind) is True
