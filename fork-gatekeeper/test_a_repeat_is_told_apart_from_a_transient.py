#!/usr/bin/env python3
"""Three byte-identical alerts, and nothing said they were the same failure.

The brief that produced this file puts the distinction plainly: a pre-release
stage that refuses on a fixed assertion an upstream merge has moved past FAILS AT
THE MOMENT OF SUCCESS and repeats every round until the assertion is repaired; a
stage that refuses because a shared checkout was busy fails once and clears.
**The two need opposite responses** — and repairing an assertion that was never
the problem is the specific mistake the brief warns about, because deleting the
assertion looks like the fix.

Nothing in the pipeline could tell them apart. `check_release_failed` opens one PR
per failing day and each is independent of the last, so the 2026-08-25,
2026-09-07 and 2026-09-08 rounds produced the same bytes three times with no
correlation anywhere. `check_publish_silence` does count consecutive rounds, but
it measures the PAGE — a consequence — and only past a threshold.

THE DISCRIMINATOR IS NOT A COUNT. Two rounds refusing in the same stage says
nothing on its own; what separates the two diagnoses is whether the SUBJECT is
the same:

    same stage, same fork  -> REPEATING_SAME_SUBJECT   (a fixed thing being outrun)
    same stage, other fork -> REPEATING_STAGE_ONLY     (an environment)

And the third state matters as much as the first two: with fewer than two
retained rounds the answer is UNDETERMINED, never "transient". One observation
cannot distinguish an incident from a generator, and calling it a transient is
the empty-denominator false clean this whole family of defects is made of.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import check_refusal_repeats as R  # noqa: E402
import record_blocked_round as REC  # noqa: E402


def _mod():
    spec = importlib.util.spec_from_file_location(
        "crf_repeat_under_test", HERE / "check_release_failed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _round(state, stamp, stages: dict, forks=()):
    """One retained round in the archive, written the way the archiver leaves it."""
    d = state / R.ARCHIVE_DIRNAME / f"{stamp}.blocked"
    d.mkdir(parents=True)
    (d / R.BLOCKED_RECORD).write_text(
        json.dumps(REC.record(stages, round_at=stamp)) + "\n", encoding="utf-8")
    if forks:
        (d / R.MERGE_RECORD).write_text(json.dumps({
            "program": "daily_merge",
            "forks": [{"repo": r, "state": s, "detail": "", "conflicts": [],
                       "checks": []} for r, s in forks]}) + "\n",
            encoding="utf-8")
    return d


# --- the two diagnoses the brief says need opposite responses ----------------

def test_the_same_fork_every_round_is_named_a_moving_target(tmp_path):
    """THE CASE THE BRIEF DESCRIBES. Three mornings, `daily_merge`, Trilinos every
    time. This will not clear on its own and the alert has to say so."""
    for day in ("2026-09-06T053001Z", "2026-09-07T053001Z", "2026-09-08T053001Z"):
        _round(tmp_path, day, {"merge_rc": 1},
               forks=[("yosys", "MERGED"), ("Trilinos", "POST_MERGE_CHECK_FAILED")])
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "REPEATING_SAME_SUBJECT", v
    assert v["rounds_with_this_stage"] == 3, v
    assert any("Trilinos" in s for s in v["shared_subjects"]), v
    assert "will not clear on its own" in v["why"]


def test_a_different_fork_each_round_is_named_an_environment(tmp_path):
    """THE OTHER DIAGNOSIS, and the one the count alone would get wrong. Same
    stage three mornings running, a different fork every time: the stage is not
    the subject. Repairing an assertion here repairs the wrong thing — which is
    exactly what the brief says not to do."""
    _round(tmp_path, "2026-09-06T053001Z", {"merge_rc": 1},
           forks=[("OpenROAD", "FETCH_STALLED")])
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1},
           forks=[("klayout", "WORKTREE_FAILED")])
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1},
           forks=[("verilator", "FETCH_STALLED")])
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "REPEATING_STAGE_ONLY", v
    assert v["shared_subjects"] == [], v
    assert "environment" in v["why"]


def test_a_partial_overlap_is_still_the_same_subject(tmp_path):
    """A generator does not have to be the only thing failing. Trilinos in all
    three rounds is a generator even when other forks come and go."""
    _round(tmp_path, "2026-09-06T053001Z", {"merge_rc": 1},
           forks=[("Trilinos", "POST_MERGE_CHECK_FAILED"), ("a", "FETCH_STALLED")])
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1},
           forks=[("Trilinos", "POST_MERGE_CHECK_FAILED")])
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1},
           forks=[("Trilinos", "POST_MERGE_CHECK_FAILED"), ("b", "WORKTREE_FAILED")])
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "REPEATING_SAME_SUBJECT", v
    assert v["shared_subjects"] == ["Trilinos POST_MERGE_CHECK_FAILED"], v


def test_the_same_fork_in_a_different_state_is_not_the_same_subject(tmp_path):
    """A fork that conflicted on Monday and stalled a fetch on Tuesday is two
    different failures wearing one name. Keying on the repo alone would call that
    a generator and send someone to look for an assertion."""
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1},
           forks=[("OpenROAD", "CONFLICT_NEEDS_HUMAN")])
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1},
           forks=[("OpenROAD", "FETCH_STALLED")])
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "REPEATING_STAGE_ONLY", v


# --- one observation is not a transient --------------------------------------

def test_one_retained_round_is_undetermined_not_transient(tmp_path):
    """THE EMPTY DENOMINATOR. Today's round is in the archive and nothing else is.
    That cannot distinguish an incident from a generator, and reporting it as a
    transient is the false clean the whole repair is about."""
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1},
           forks=[("Trilinos", "POST_MERGE_CHECK_FAILED")])
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "UNDETERMINED", v
    assert "not evidence of a transient" in v["why"]


def test_a_stage_that_refused_once_out_of_several_rounds_says_once(tmp_path):
    """The genuinely-single occurrence, which IS distinguishable — but only
    because other rounds were retained to compare against."""
    _round(tmp_path, "2026-09-06T053001Z", {"selftest_rc": 1})
    _round(tmp_path, "2026-09-07T053001Z", {"selftest_rc": 1})
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1})
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["verdict"] == "ONCE", v
    assert R.verdict(R.history(tmp_path)[1],
                     "fork self-checks")["verdict"] == "REPEATING_STAGE_ONLY"


def test_a_missing_archive_is_norecord_not_no_repeats(tmp_path):
    """"Could not ask" and "asked, and the answer is no" are different states."""
    rc, text = R.report(tmp_path / "nothing", "daily_merge")
    assert rc == R.RC_NORECORD
    assert "COULD NOT BE ASKED" in text, text
    assert "not the same as it not having happened" in text, text


def test_each_stage_is_counted_on_its_own(tmp_path):
    """A round where two stages refused must not lend one stage's history to the
    other. `run_tick`'s exit code names one stage; the record names all of them."""
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1, "selftest_rc": 1})
    _round(tmp_path, "2026-09-08T053001Z", {"selftest_rc": 1})
    rounds = R.history(tmp_path)[1]
    assert R.verdict(rounds, "daily_merge")["rounds_with_this_stage"] == 1
    assert R.verdict(rounds, "fork self-checks")["rounds_with_this_stage"] == 2


def test_a_round_with_no_per_fork_record_still_counts_as_a_round(tmp_path):
    """`daily-merge.json` is absent when daily_merge crashed before writing it.
    Dropping that round would make a generator look like a transient."""
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1})
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1})
    v = R.verdict(R.history(tmp_path)[1], "daily_merge")
    assert v["rounds_with_this_stage"] == 2, v
    assert v["verdict"] == "REPEATING_STAGE_ONLY", v
    assert "no round retained a per-fork record" in v["why"], v


def test_a_malformed_retained_record_is_skipped_not_fatal(tmp_path):
    """The archive is on disk and disks truncate. One unreadable round must not
    take the verdict on the others with it."""
    _round(tmp_path, "2026-09-07T053001Z", {"merge_rc": 1})
    bad = tmp_path / R.ARCHIVE_DIRNAME / "2026-09-08T053001Z.blocked"
    bad.mkdir(parents=True)
    (bad / R.BLOCKED_RECORD).write_text("{ truncated", encoding="utf-8")
    rc, rounds = R.history(tmp_path)
    assert rc == R.RC_OK
    assert len(rounds) == 1, rounds


# --- it reaches the alert, where the decision is made ------------------------

def _raise(m, monkeypatch, *args):
    seen = {}

    def _capture(summary, report_md, round_at="undated", kind="release-failed"):
        seen["summary"], seen["body"] = summary, report_md
        return True, "captured"

    monkeypatch.setattr(m, "_default_escalate", _capture)
    rc = m.main(["--round-at", "2026-09-08T05:30:01+08:00", *args])
    return rc, seen.get("summary", ""), seen.get("body", "")


def test_the_alert_leads_with_the_repeat_verdict(tmp_path, monkeypatch):
    """FIRST in the detail, not last — it is the triage decision itself, the same
    reason `failure.retryable` is surfaced by name for a release failure."""
    for day in ("2026-09-06T053001Z", "2026-09-07T053001Z", "2026-09-08T053001Z"):
        _round(tmp_path, day, {"merge_rc": 1},
               forks=[("Trilinos", "POST_MERGE_CHECK_FAILED")])
    (tmp_path / "daily-merge.txt").write_text(
        "daily_merge: 38 fork(s), 0 merged, 1 need a human\n"
        "  Trilinos       POST_MERGE_CHECK_FAILED  1 of 1 check(s) red\n",
        encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path))
    m = _mod()
    rc, _, body = _raise(m, monkeypatch, "--tick-exit", "4")
    assert rc == m.RC_ESCALATED
    quoted = body.split("## What daily_merge said")[1].split("```")[1]
    assert quoted.lstrip().startswith("[REPEATING_SAME_SUBJECT]"), quoted
    assert "Trilinos" in quoted, quoted


def test_the_alert_says_undetermined_on_a_first_occurrence(tmp_path, monkeypatch):
    """It must not manufacture a verdict from one round. The alert that says
    "UNDETERMINED, only one retained round" is more use than one that guesses."""
    _round(tmp_path, "2026-09-08T053001Z", {"merge_rc": 1})
    (tmp_path / "daily-merge.txt").write_text("daily_merge: 1 fork\n",
                                              encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path))
    m = _mod()
    _, _, body = _raise(m, monkeypatch, "--tick-exit", "4")
    assert "UNDETERMINED" in body, body


def test_a_release_failure_body_is_untouched(tmp_path, monkeypatch):
    """CONTROL. Exit 5 has its own retryable field and its own archive; the repeat
    verdict is a pre-release-stage question and must not appear there."""
    (tmp_path / "daily-release.json").write_text(json.dumps(
        {"program": "daily_release",
         "failure": {"category": "buildkit_solve_failed", "retryable": False}}),
        encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path))
    m = _mod()
    _, summary, body = _raise(m, monkeypatch, "--tick-exit", "5")
    assert summary.startswith("release failed"), summary
    for v in R.VERDICTS:
        assert v not in body, (v, body)


def test_the_verdict_vocabulary_is_closed(tmp_path):
    """Every verdict the program can print is declared, so the alert's own
    "What to check" section cannot describe a state that no longer exists."""
    src = (HERE / "check_release_failed.py").read_text(encoding="utf-8")
    for v in ("REPEATING_SAME_SUBJECT", "REPEATING_STAGE_ONLY", "UNDETERMINED"):
        assert v in src, f"{v} is not explained where the reader is sent"
    assert set(R.VERDICTS) == {"REPEATING_SAME_SUBJECT", "REPEATING_STAGE_ONLY",
                               "ONCE", "UNDETERMINED"}
