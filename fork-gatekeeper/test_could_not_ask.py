"""#48 / #49 — "could not ask" must not arrive as "the answer is no".

Both sites pre-date #47 and are the same sentence it spent five rounds removing
from the containment path, sitting one function upstream and one program across.

#49  `_tags_by_date` returned `[]` from all three failure paths — and `[]` is
     also a legitimate answer ("this repository has no tags"). Its own docstring
     said "[] if it could not be asked", naming the conflation without closing
     it. `_releases` merged that list, so a repository whose tag feed could not
     be read presented as one with nothing to be behind, in the `measured`
     status.

#48  `pr_precheck` mapped a FAILED compare onto the same `0` that means "the
     base has not moved" and the same `[]` that means "the base added nothing".
     BOTH halves of the verdict were satisfiable by one non-measurement, and the
     direction is the dangerous one: a reviewer-side gate goes from stop to go on
     a network blip. Every other instance of this shape degraded a number on a
     page; this one degrades a decision about whether a change is safe to land.

Each fix is paired with its ACCEPT case, because a refusal that fires on the
healthy path is not a fix — it is the same gate switched off from the other end.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import discover_forks as df           # noqa: E402
import pr_precheck as pp              # noqa: E402


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


_TAGS_OK = json.dumps({"data": {"repository": {"refs": {"nodes": [
    {"name": "v2.0", "target": {"committedDate": "2026-05-01T00:00:00Z"}},
]}}}})
_TAGS_NONE = json.dumps({"data": {"repository": {"refs": {"nodes": []}}}})


# ── #49 _tags_by_date: None means could not ask ─────────────────────────────
def test_a_failed_process_is_not_an_empty_tag_list(monkeypatch):
    def boom(*a, **k):
        raise OSError("no gh on PATH")
    monkeypatch.setattr(df.subprocess, "run", boom)
    assert df._tags_by_date("them/Tool") is None


def test_a_nonzero_exit_is_not_an_empty_tag_list(monkeypatch):
    monkeypatch.setattr(df.subprocess, "run",
                        lambda *a, **k: _R(rc=1, err="HTTP 502"))
    assert df._tags_by_date("them/Tool") is None


def test_unparseable_output_is_not_an_empty_tag_list(monkeypatch):
    monkeypatch.setattr(df.subprocess, "run",
                        lambda *a, **k: _R(rc=0, out="<html>rate limited"))
    assert df._tags_by_date("them/Tool") is None


def test_a_repository_with_no_tags_still_answers_with_an_empty_list(monkeypatch):
    """THE ACCEPT CASE. `[]` has to keep meaning "asked, and there are none",
    or the distinction is just a different single value."""
    monkeypatch.setattr(df.subprocess, "run",
                        lambda *a, **k: _R(rc=0, out=_TAGS_NONE))
    assert df._tags_by_date("them/Tool") == []


def test_tags_are_still_returned_normally(monkeypatch):
    monkeypatch.setattr(df.subprocess, "run",
                        lambda *a, **k: _R(rc=0, out=_TAGS_OK))
    got = df._tags_by_date("them/Tool")
    assert got and got[0]["tag"] == "v2.0"


# ── #49 _releases propagates it ─────────────────────────────────────────────
def test_neither_source_answering_is_not_an_empty_release_list(monkeypatch):
    """The failure the ledger showed: a tool nobody could ask about reported
    zero releases behind, under `measured`."""
    monkeypatch.setattr(df, "gh", lambda p: {"_err": "HTTP 502"})
    monkeypatch.setattr(df, "_tags_by_date", lambda u, limit=30: None)
    assert df._releases("them/Tool") is None


def test_a_project_with_no_versions_at_all_is_still_an_empty_list(monkeypatch):
    """THE ACCEPT CASE, and the one that separates the two: both sources
    answered, and there is genuinely nothing."""
    monkeypatch.setattr(df, "gh", lambda p: [])
    monkeypatch.setattr(df, "_tags_by_date", lambda u, limit=30: [])
    assert df._releases("them/Tool") == []


def test_one_source_answering_is_enough(monkeypatch):
    """A tag feed that could not be read must not discard releases that WERE
    read — the refusal is for when nothing answered, not for a partial."""
    monkeypatch.setattr(df, "gh", lambda p: [
        {"tag_name": "v1.0", "published_at": "2026-01-01T00:00:00Z",
         "prerelease": False}])
    monkeypatch.setattr(df, "_tags_by_date", lambda u, limit=30: None)
    got = df._releases("them/Tool")
    assert got is not None and got[0]["tag"] == "v1.0"


# ── #48 pr_precheck: an unread compare is its own outcome ───────────────────
_PR = {"state": "OPEN", "body": "Closes #7",
       "base": {"ref": "main"}, "head": {"ref": "topic"}}


def _precheck_with(monkeypatch, compare):
    """`_gh_json` answers the PR call, then the compare call."""
    seen = {"n": 0}

    def fake(path):
        seen["n"] += 1
        if "/compare/" in path:
            return compare
        return {"state": "open", "body": "Closes #7",
                "base": {"ref": "main"}, "head": {"ref": "topic"}}
    monkeypatch.setattr(pp, "_gh_json", fake)
    return pp.precheck("vibeic/Tool", 1)


def test_a_failed_compare_is_neither_ok_nor_a_silent_pass(monkeypatch):
    """The whole issue. `or {}` made a network blip indistinguishable from "the
    base has not moved", and a reviewer-side gate went from stop to go."""
    rep = _precheck_with(monkeypatch, None)
    assert rep["verdict"] == "UNMEASURED", rep
    assert rep["code"] != 0, "an unread compare exits 0 — it reads as an approval"
    assert rep["code"] not in (1, 2), (
        "it borrows another verdict's code, so a caller cannot tell it apart")


def test_an_unmoved_base_is_still_ok(monkeypatch):
    """THE ACCEPT CASE. A refusal that also fires on the healthy path is the
    same gate switched off from the other end."""
    rep = _precheck_with(monkeypatch, {"ahead_by": 0, "commits": []})
    assert rep["verdict"] == "OK" and rep["code"] == 0, rep


def test_an_advanced_base_still_asks_for_review(monkeypatch):
    rep = _precheck_with(monkeypatch, {"ahead_by": 3, "commits": [
        {"sha": "a" * 40, "commit": {"message": "unrelated work"}}]})
    assert rep["verdict"] == "REVIEW" and rep["code"] == 1, rep


def test_a_superseding_base_commit_is_still_caught(monkeypatch):
    """LOAD-BEARING: the redundancy loop must still see real commits. It reads
    `base_only`, which the fix changed the type of."""
    rep = _precheck_with(monkeypatch, {"ahead_by": 1, "commits": [
        {"sha": "b" * 40, "commit": {"message": "fix: thing\n\nCloses #7"}}]})
    assert rep["verdict"] == "REDUNDANT_RISK" and rep["code"] == 2, rep
    assert rep["base_already_closes"], rep


def test_the_unmeasured_outcome_is_documented_in_the_exit_contract():
    """A fourth exit code nobody wrote down is a fourth way to be surprised."""
    src = (HERE / "pr_precheck.py").read_text(encoding="utf-8")
    head = src.split('"""')[1]
    assert "UNMEASURED" in head and "(3)" in head, (
        "the new outcome is not in the module's own exit-code table")
