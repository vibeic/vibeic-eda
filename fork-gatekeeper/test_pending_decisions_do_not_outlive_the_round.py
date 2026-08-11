"""A file named `pending` must describe THIS round, or it is a false statement.

MEASURED 2026-08-12. `ai_decisions_pending.json` on this host held one case:

    branch 4ede5f5b17f1cf82c7f188cf9a51905fb68dc750
    direction "upstream -> our mainline", 56 commits,
    conflicting src/tap/include/tap/tapcell.h, src/tap/src/tapcell.tcl

and that merge was ALREADY IN HEAD:

    $ git -C ~/vibe-ic-forks/OpenROAD merge-base --is-ancestor \
          4ede5f5b17f1cf82c7f188cf9a51905fb68dc750 HEAD ; echo $?
    0

The decision had been made, the conflict resolved, the merge committed and
pushed. The file still said it was pending, and had said so for a day.

WHY IT SURVIVES A ROUND THAT DISAGREES WITH IT
----------------------------------------------
`step2b_ai_decisions` writes the file only when it has cases:

    if not cases:
        return {"cases": 0, "invoked": False}     # <- returns BEFORE the write
    ...
    pending.write_text(json.dumps(cases, indent=2) + "\n")

so the zero-case round -- the round that PROVES nothing is pending -- is exactly
the round that leaves yesterday's cases untouched. Nothing else in the tree
removes it: `grep -rn pending run_0530.sh _gate_dispatch.sh` is empty and
`daily_0530.py` never unlinks. The file is written once and lives forever.

That is the same shape as a baseline register that outlives its truth, with the
extra sting that the next reader is a decision-maker: the round hands this file
and its brief to an AI whose job is to resolve what is in it. A resolved case
that reads as open invites the merge to be re-litigated, and a genuinely new
case arriving later is indistinguishable from the fossil sitting next to it.

WHAT THE FIX MUST DO -- and what it must NOT
--------------------------------------------
Must: make the file a statement about the CURRENT round, so a round with no
cases leaves nothing claiming otherwise. Must not: delete the history. The
narrative of what was decided lives in `ai_decisions.log`, which is a log and
is correctly append-only; this is the work queue, and a work queue that cannot
be emptied is not a queue.
"""
from __future__ import annotations

import importlib.util
import json
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

_CASE = {
    "fork": "OpenROAD",
    "path": "/x/OpenROAD",
    "branch": "4ede5f5b17f1cf82c7f188cf9a51905fb68dc750",
    "direction": "upstream -> our mainline",
    "commits": 56,
    "conflicting_files": ["src/tap/include/tap/tapcell.h"],
    "commits_detail": [{"sha": "4ede5f5b17f1", "subject": "Merge pull request #11114"}],
}


def _report_with_a_case():
    return {"OpenROAD": {"conflicted": [_CASE]}}


def test_a_round_with_no_cases_leaves_nothing_claiming_one_is_pending(tmp_path):
    """The regression itself: yesterday's queue must not survive today's round."""
    state = tmp_path / "state"
    state.mkdir()

    # Round 1: a real conflict. The queue is written -- correctly.
    M.step2b_ai_decisions(_report_with_a_case(), state, apply=False)
    pending = state / "ai_decisions_pending.json"
    assert pending.is_file(), "a round WITH a case must write the queue"
    assert json.loads(pending.read_text())[0]["branch"] == _CASE["branch"]

    # Round 2: the case was resolved, so today's round finds nothing.
    M.step2b_ai_decisions({"OpenROAD": {"conflicted": []}}, state, apply=False)

    if pending.exists():
        left = json.loads(pending.read_text())
        assert left == [], (
            "a round that found NO conflicts left yesterday's case on disk still "
            f"named pending: {left!r}. This is the measured 2026-08-12 state -- "
            "the queue outlived the decision that emptied it.")


def test_the_brief_does_not_outlive_the_queue_either(tmp_path):
    """The brief is the text a decision-maker is handed. Same rule."""
    state = tmp_path / "state"
    state.mkdir()

    M.step2b_ai_decisions(_report_with_a_case(), state, apply=False)
    brief = state / "ai_decisions_brief.txt"
    assert brief.is_file() and _CASE["branch"] in brief.read_text()

    M.step2b_ai_decisions({"OpenROAD": {"conflicted": []}}, state, apply=False)

    if brief.exists():
        assert _CASE["branch"] not in brief.read_text(), (
            "the handoff brief still instructs a decision-maker to resolve a "
            "case that today's round proved is not pending")


def test_a_round_with_cases_still_writes_them(tmp_path):
    """The obverse: the fix must not achieve emptiness by never writing.

    Without this, deleting the write would pass the two tests above.
    """
    state = tmp_path / "state"
    state.mkdir()

    res = M.step2b_ai_decisions(_report_with_a_case(), state, apply=False)
    assert res["cases"] == 1

    pending = state / "ai_decisions_pending.json"
    assert pending.is_file(), "a genuine case must reach the queue"
    got = json.loads(pending.read_text())
    assert len(got) == 1 and got[0]["fork"] == "OpenROAD"

    brief = (state / "ai_decisions_brief.txt").read_text()
    assert "OpenROAD" in brief and "src/tap/include/tap/tapcell.h" in brief


def test_the_zero_case_round_still_reports_zero_not_a_failure(tmp_path):
    """Emptying the queue is a normal, quiet outcome -- not an error state."""
    state = tmp_path / "state"
    state.mkdir()
    res = M.step2b_ai_decisions({"OpenROAD": {"conflicted": []}}, state, apply=False)
    assert res["cases"] == 0 and res["invoked"] is False
