#!/usr/bin/env python3
"""The publish-silence alarm must not be counted as open delivery work.

Every other item `fork_github_convergence` counts is something the fleet owes
somebody. The alarm is the opposite: the round telling a human that the public
page has stopped publishing. Counting it made the alarm block the very thing it
reports on — measured 2026-08-21, where #138 ("fork page has not published for
2 consecutive rounds") was the sole item keeping convergence non-zero, which
skipped build_page, which extended the silence the PR exists to report.

I had already convinced myself this could not happen. Moving the alarm from
vibe-ic to vibeic-eda, I read the registry construction, saw the denominator
built from registered forks plus vibe-ic, checked the round's sidecar, saw only
`vibeic/vibe-ic` in `items`, and concluded vibeic-eda was outside it. Line 1069
says `canonical_repositories + [VIBEIC_EDA_REPOSITORY]`. The sidecar showed no
vibeic-eda item because no vibeic-eda PR happened to be open at that moment —
one observation, read as a rule.

EXCLUDED IS NOT HIDDEN. The count `alarm_items_excluded`, the list
`alarm_items`, and the summary line all name what was set aside. A reader who
sees "converged — 0 open" must be able to tell that from "converged, and one
notice was set aside".
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import fork_github_convergence as F  # noqa: E402


def _item(**over):
    item = {"repository": F.VIBEIC_EDA_REPOSITORY, "type": "pull_request",
            "number": 138, "head_ref": "publish-silence-2026-08-20",
            "title": "fork page has not published for 2 consecutive rounds"}
    item.update(over)
    return item


def test_an_alarm_on_this_repo_is_not_open_work():
    assert F._is_alarm(_item()) is True


@pytest.mark.parametrize("over", [
    {"repository": "vibeic/vibe-ic"},                   # right branch, wrong repo
    {"repository": "vibeic/yosys"},
    {"head_ref": "eda-fork-sync-2026-08-20"},           # right repo, ordinary work
    {"head_ref": "fix/publish-silence-thing"},          # prefix must ANCHOR
    {"head_ref": ""},
    {"head_ref": None},
])
def test_ordinary_work_is_still_counted(over):
    """LOAD-BEARING. Every real delivery must keep blocking the publish; an
    over-broad match here would let genuine unfinished work through as 'an
    alarm'."""
    assert F._is_alarm(_item(**over)) is False


def test_the_match_is_repository_and_branch_not_title_or_author():
    """Title and author are free text a human can reproduce by accident. The
    branch name is written by `check_publish_silence` itself."""
    assert F._is_alarm(_item(title="something else", author="someone")) is True
    assert F._is_alarm(_item(head_ref="topic",
                             title="fork page has not published")) is False


def test_the_exclusion_is_reported_not_silent():
    """A filter nobody can see becomes a blind spot: '0 open' would then be
    indistinguishable from 'one was dropped'."""
    src = (_HERE / "fork_github_convergence.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert '"alarm_items_excluded"' in code, "the count must reach the record"
    assert '"alarm_items"' in code, "the excluded items themselves must too"
    assert "NOT counted as open work" in code, (
        "the human-readable summary must say what was set aside")
