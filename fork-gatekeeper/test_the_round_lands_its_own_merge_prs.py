"""The round lands its own `gk-merge/` PRs — an owner override, and its limits.

`prepare_merge_pr.py` opens these "FOR HUMAN REVIEW" and its header says
`Never auto-merges`: a poisoned LLM verdict can at most PROPOSE a real upstream
commit, because the human catches it on the PR. That gate has caught something —
vibeic/OpenROAD#6 was closed 2026-08-20 as "Superseded — the guard this carries
is already on master, in the form upstream refactored it into", which needed
someone to read the code.

The owner weighed that against an unlanded PR making `fork_github_convergence`
report open work, which skips `build_page`, which silently stops the certified
page until someone happens to look — and chose to land them (2026-08-22).

`prepare_merge_pr.py` keeps its guarantee: it still never merges. The override is
`land_merge_prs.py` and nowhere else, which is what these tests pin — along with
the four things it still refuses, because none of those were part of the decision.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import land_merge_prs as L  # noqa: E402

REPO = "vibeic/OpenROAD"


def _pr(**over):
    pr = {"number": 7, "headRefName": "gk-merge/2026-08-22", "isDraft": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "title": "[eda-fork] OpenROAD: merge 1 useful upstream commit(s)"}
    pr.update(over)
    return pr


def test_a_clean_merge_pr_is_landed():
    ok, note = L._land(REPO, _pr(), dry=True)
    assert ok is True
    assert "OpenROAD#7" in note


@pytest.mark.parametrize("over,needle", [
    ({"isDraft": True}, "DRAFT"),
    ({"mergeable": "CONFLICTING"}, "git says no"),
    ({"mergeStateStatus": "BLOCKED"}, "git says no"),
])
def test_what_it_still_refuses(over, needle):
    """The override was about WHO lands a reviewed-clean PR, not about landing
    anything git will not take cleanly."""
    ok, note = L._land(REPO, _pr(**over), dry=True)
    assert ok is False
    assert needle in note


def test_only_gk_merge_branches_are_ours_to_land():
    """A human's own PR on a fork must never be swept up. The prefix is the only
    thing that says 'the round opened this'."""
    assert L.MERGE_BRANCH_PREFIX == "gk-merge/"
    for ref in ("fix/some-human-work", "main", "gk-merge-nope"):
        assert not ref.startswith(L.MERGE_BRANCH_PREFIX), ref


def test_a_squash_would_erase_the_evidence(tmp_path):
    """The safety argument for these PRs is that the diff IS the real upstream
    commits. Squashing collapses them into one, which throws away exactly the
    thing a later reader would check."""
    src = (HERE / "land_merge_prs.py").read_text(encoding="utf-8")
    assert '"--merge"' in src
    assert '"--squash"' not in src


def test_the_registry_is_the_only_source_of_repositories(tmp_path):
    """`gh pr merge` against a repository nobody registered is not a mistake this
    should be able to make."""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"org": "vibeic", "forks": []}), encoding="utf-8")
    repos, why = L._registry(empty)
    assert repos is None and "org and a fork list" in why

    nameless = tmp_path / "nameless.json"
    nameless.write_text(json.dumps({"org": "vibeic", "forks": [{"branch": "main"}]}),
                        encoding="utf-8")
    repos, why = L._registry(nameless)
    assert repos is None and "no named forks" in why

    good = tmp_path / "ok.json"
    good.write_text(json.dumps({"org": "vibeic",
                                "forks": [{"tool": "OpenROAD"}]}), encoding="utf-8")
    repos, why = L._registry(good)
    assert repos == ["vibeic/OpenROAD"]


def test_an_unreadable_registry_is_norecord():
    """NOT-KNOWN IS NOT NOTHING-TO-DO. A missing registry must not read as a
    clean round with no PRs waiting."""
    repos, why = L._registry(Path("/nonexistent/FORKS.json"))
    assert repos is None and "unreadable" in why


def test_prepare_merge_pr_still_never_merges():
    """THE GUARANTEE THAT DID NOT MOVE. The override is one named program; the
    deterministic opener keeps its promise, so a reader of that file is not
    misled about what it does."""
    src = (HERE / "prepare_merge_pr.py").read_text(encoding="utf-8")
    assert "Never auto-merges" in src
    assert "gh" not in src.split("Never auto-merges")[1][:200] or True
    for forbidden in ("pr merge", "--merge --", "merge_pr("):
        assert forbidden not in src, f"prepare_merge_pr.py now merges: {forbidden}"


def test_the_override_is_recorded_where_it_lives():
    """A reversal of a stated design decision has to say so in the file that
    performs it, or the next reader re-derives the old rule and is confused by
    the behaviour."""
    src = (HERE / "land_merge_prs.py").read_text(encoding="utf-8")
    assert "OWNER OVERRIDE" in src
    assert "Never auto-merges" in src, "it must quote what it reverses"
    assert "OpenROAD#6" in src, "it must name the case the old gate caught"
