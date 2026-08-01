"""The brief must name the side that is actually at risk.

`step2b_ai_decisions` reads `rep["conflicted"]`, which only `step2_ours` filled,
so an UPSTREAM conflict printed "needs a human" and reached no decision-maker.
Routing it there is the fix. But the brief hard-coded the our-branch framing:

    "Every branch below carries commits of OURS that are not on our master"
    f"({c['commits']} of our commits)"
    "A conflict resolved by dropping our fix ..."

and an upstream case has UPSTREAM's commit count, in a field that was literally
named `our_commits`. The `direction` field was carried into `cases` and never
rendered — the one field distinguishing the two was the one the brief dropped.

That is the defect being fixed, moved one step later: the case reaches a
decision-maker and the decision is made on a false description. Worse than the
honest `needs_human` it replaces, because a printed line nobody acts on at least
stays visible.

The two directions put OPPOSITE sides at risk: `our branch ->` risks abandoning
our fix; `upstream ->` risks dropping an upstream contribution — the invariant
the owner states first.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("daily_0530", HERE / "daily_0530.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_0530"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()

_UPSTREAM = {
    "fork": "OpenSTA", "path": "/x/OpenSTA", "branch": "upstream/main",
    "direction": "upstream -> our mainline", "commits": 11,
    "conflicting_files": ["a.cc"],
    "commits_detail": [{"sha": "deadbeef1234", "subject": "upstream fix",
                        "author": "someone"}],
    "merge_stderr": "CONFLICT",
}
_OURS = {
    "fork": "yosys", "path": "/x/yosys", "branch": "vibeic/fix",
    "direction": "our branch -> our mainline", "commits": 3,
    "conflicting_files": ["b.cc"],
    "commits_detail": [{"sha": "cafebabe5678", "subject": "our fix",
                        "author": "reyerchu"}],
    "merge_stderr": "CONFLICT",
}


def test_the_direction_is_rendered_for_each_case():
    """The field existed and was never printed."""
    brief = M._handoff_brief([_UPSTREAM, _OURS])
    assert "upstream -> our mainline" in brief
    assert "our branch -> our mainline" in brief


def test_an_upstream_case_is_not_called_ours():
    """The count belongs to upstream. Calling it 'our commits' tells the
    decision-maker the opposite of the truth about 11 commits."""
    brief = M._handoff_brief([_UPSTREAM])
    assert "11 of upstream's commits" in brief, brief
    assert "of our commits" not in brief, brief


def test_an_our_branch_case_is_still_called_ours():
    """The accept case. A fix that renamed everything neutrally would satisfy
    the test above and lose the information in the other direction."""
    brief = M._handoff_brief([_OURS])
    assert "3 of our commits" in brief, brief


def test_the_intro_does_not_assert_one_direction_for_every_case():
    """It said 'Every branch below carries commits of OURS' regardless."""
    brief = M._handoff_brief([_UPSTREAM])
    assert "carries commits of OURS" not in brief, brief


def test_the_drop_rule_names_both_sides():
    """'dropping our fix' points at preserving OUR side, which is the wrong
    instruction for an upstream conflict."""
    brief = M._handoff_brief([_UPSTREAM, _OURS])
    assert "upstream contribution" in brief, brief


def test_the_commit_list_is_rendered_from_the_neutral_field():
    brief = M._handoff_brief([_UPSTREAM])
    assert "deadbeef1234" in brief and "upstream fix" in brief


def test_a_case_with_no_direction_still_renders():
    """Defensive: an older entry shape must not crash the tick. It falls back
    to the our-branch reading, which is what it used to mean."""
    legacy = {k: v for k, v in _OURS.items() if k != "direction"}
    legacy["our_commits"] = legacy.pop("commits_detail")
    brief = M._handoff_brief([legacy])
    assert "our branch -> our mainline" in brief
    assert "cafebabe5678" in brief, "the legacy field name stopped being read"
