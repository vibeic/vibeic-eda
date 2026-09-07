#!/usr/bin/env python3
"""tick.log said "1 need a human" and never said which fork.

`run_tick.sh`'s `[merge]` block logs daily_merge's summary line, which gives a
COUNT, and then named the offenders with
`grep -E "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM"` — a hand-written list of state
names, against a program whose failure vocabulary has kept growing.

MEASURED HERE, from daily_merge's own AST rather than from a list in this file:
26 states can appear on a row, 3 of them clean, so 23 mean the fork was not
published — and that pattern names 8. The fifteen it misses are every STALLED,
UNMEASURABLE, UNKNOWN and REJECTED outcome, i.e. exactly the states that mean
"the question was not answered". The pattern was written from failures somebody
had already seen; what it cannot see is what nobody had seen yet.

`tick.log` is append-only and never rotated, so it is the one place where three
mornings of the same failure could have been compared. On 2026-08-25, 2026-09-07
and 2026-09-08 the round exited 4 and that comparison was not possible from the
log, from the alert, or from anywhere else.

The fix inverts on the CLEAN set, which is closed and stable, so an unrecognised
state is REPORTED rather than dropped — the safe direction, and the opposite of
the one the grep failed in.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "merge_findings.sh"
TICK = HERE / "run_tick.sh"

CLEAN = ("MERGED", "ALREADY_CURRENT", "WOULD_MERGE")
OLD_PATTERN = re.compile("CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM")


def _row_states():
    """Every literal daily_merge can put in a row's `state`, from its own AST.

    A hand-written list here would go stale the first time a new failure mode is
    added, which is the defect itself one level up.
    """
    tree = ast.parse((HERE / "daily_merge.py").read_text(encoding="utf-8"))
    found = set()

    def _lits(node):
        for n in ast.walk(node):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", n.value)):
                found.add(n.value)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == "state":
                    _lits(kw.value)
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "state":
                    _lits(n.value)
    # main() builds these two from the BRANCH_* lookup status, not from `state=`
    found |= {"NO_BRANCH_AT_PIN", "BRANCH_LOOKUP_UNKNOWN"}
    found.discard("?")
    return found


def _merge_out(tmp_path, states):
    lines = [f"daily_merge: {len(states)} fork(s), 1 merged, 3 upstream "
             f"commit(s) taken, {len(states) - len(CLEAN)} need a human"]
    for i, s in enumerate(sorted(states)):
        lines.append(f"  fork{i:<10} {s:<30} detail for {s.lower()}")
        if s == "CONFLICT_NEEDS_HUMAN":
            lines.append("      conflict: src/rcx/src/extmain_v2.cpp")
        if s == "MERGED":
            lines.append("      check ok   dup-logger-ids: 0 duplicates")
    lines.append("[NEEDS HUMAN] fork(s) were not published; each is left at its "
                 "previous tip")
    p = tmp_path / "daily-merge.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run(path):
    r = subprocess.run([str(SCRIPT), str(path)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


# --- the case this exists for ------------------------------------------------

def test_every_unclean_state_is_named(tmp_path):
    """THE REGRESSION. One row per state daily_merge can produce; every state that
    means "not published" must appear in what the round logs."""
    states = _row_states()
    out = _run(_merge_out(tmp_path, states))
    unclean = sorted(s for s in states if s not in CLEAN)
    missing = [s for s in unclean if s not in out]
    assert not missing, f"{len(missing)} of {len(unclean)} unpublished states are "\
                        f"still invisible in tick.log: {missing}"


def test_the_state_name_grep_misses_on_the_very_same_input(tmp_path):
    """BOTH DIRECTIONS, ON ONE ARM. The exact pipeline `run_tick.sh` used, run over
    the exact file the new filter is run over above. This is the before-and-after
    in a single measurement: the old command cannot name most of the forks, the new
    one names all of them, and neither claim rests on the other arm existing."""
    states = _row_states()
    out_file = _merge_out(tmp_path, states)
    unclean = sorted(s for s in states if s not in CLEAN)

    old = subprocess.run(
        ["grep", "-E", "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM", str(out_file)],
        capture_output=True, text=True, timeout=60).stdout
    new = _run(out_file)

    old_missing = [s for s in unclean if s not in old]
    new_missing = [s for s in unclean if s not in new]
    assert not new_missing, new_missing
    assert len(old_missing) >= 15, (
        f"the old grep missed {len(old_missing)} of {len(unclean)}; the "
        f"measurement this change is justified by has moved")


def test_the_old_pattern_really_did_miss_most_of_them(tmp_path):
    """The measurement this change is justified by, asserted rather than claimed.
    If a later edit narrows daily_merge's states back down to what the pattern
    covered, this goes red and the justification above needs rewriting."""
    unclean = sorted(s for s in _row_states() if s not in CLEAN)
    missed = [s for s in unclean if not OLD_PATTERN.search(s)]
    assert len(missed) > len(unclean) // 2, (
        f"only {len(missed)} of {len(unclean)} were missed by the old pattern")


def test_the_unanswered_states_are_the_ones_that_were_missed(tmp_path):
    """The shape of the gap, not just its size: every state the old pattern could
    not see is a STALLED / UNMEASURABLE / UNKNOWN / REJECTED / NOT_FAST_FORWARD
    outcome — "the question was not answered", which is the class this repo keeps
    reading as silence."""
    unclean = sorted(s for s in _row_states() if s not in CLEAN)
    missed = [s for s in unclean if not OLD_PATTERN.search(s)]
    unanswered = re.compile("STALLED|UNMEASURABLE|UNKNOWN|REJECTED|"
                            "UNCONFIRMED|NOT_FAST_FORWARD|CHANGED|NO_BRANCH")
    stragglers = [s for s in missed if not unanswered.search(s)]
    assert not stragglers, stragglers


# --- the other tail ----------------------------------------------------------

def test_a_clean_round_logs_nothing(tmp_path):
    """An alarm channel that speaks on healthy rounds is one nobody reads."""
    p = tmp_path / "daily-merge.txt"
    p.write_text("daily_merge: 38 fork(s), 2 merged, 9 upstream commit(s) "
                 "taken, 0 need a human\n"
                 "  yosys          MERGED                 took 3 commit(s)\n"
                 "  klayout        ALREADY_CURRENT        level with upstream\n",
                 encoding="utf-8")
    assert _run(p).strip() == ""


def test_a_clean_fork_is_never_reported(tmp_path):
    out = _run(_merge_out(tmp_path, _row_states()))
    for s in CLEAN:
        assert s not in out, (s, out)


def test_a_clean_forks_own_check_line_is_not_orphaned(tmp_path):
    """The indented lines belong to the row above them. Printing a MERGED fork's
    passing check under `[merge]` with no row would read as a finding."""
    out = _run(_merge_out(tmp_path, _row_states()))
    assert "dup-logger-ids" not in out, out


def test_an_unclean_forks_conflict_lines_are_kept(tmp_path):
    """...and the same rule must not drop the detail that IS the diagnosis."""
    out = _run(_merge_out(tmp_path, _row_states()))
    assert "src/rcx/src/extmain_v2.cpp" in out, out


def test_a_state_this_script_has_never_heard_of_is_reported(tmp_path):
    """Inverted on the CLEAN set on purpose: a new failure mode is reported the
    day it is added, without anyone remembering to widen a pattern."""
    p = tmp_path / "daily-merge.txt"
    p.write_text("daily_merge: 1 fork(s), 0 merged\n"
                 "  newfork        A_STATE_INVENTED_TOMORROW  who knows\n",
                 encoding="utf-8")
    assert "A_STATE_INVENTED_TOMORROW" in _run(p)


def test_a_crash_is_not_silence(tmp_path):
    """daily_merge's stderr lands in the same file. A traceback means the merge
    did not happen, and reading it as a clean round is the whole failure family."""
    p = tmp_path / "daily-merge.txt"
    p.write_text("Traceback (most recent call last):\n"
                 "  File \"daily_merge.py\", line 1, in <module>\n"
                 "KeyError: 'forks'\n", encoding="utf-8")
    out = _run(p)
    assert "Traceback" in out and "KeyError" in out, out


def test_a_missing_output_file_says_so(tmp_path):
    out = _run(tmp_path / "not-there.txt")
    assert "MISSING" in out, out


# --- it is actually wired ----------------------------------------------------

def test_the_round_logs_through_it():
    src = TICK.read_text(encoding="utf-8")
    assert "merge_findings.sh" in src
    assert 'grep -E "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM"' not in src, (
        "the state-name grep is still the thing naming the forks")


def test_it_is_executable():
    assert SCRIPT.stat().st_mode & 0o111, "run_tick calls it directly"
