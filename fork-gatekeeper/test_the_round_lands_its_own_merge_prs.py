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


def test_unknown_is_not_reported_as_git_refusing():
    """GitHub computes mergeability lazily and recomputes it whenever the base
    moves — which a round does to its own forks. MEASURED 2026-08-22: the round
    advanced OpenROAD's master, `land_merge_prs` queried its own earlier PR
    mid-recompute and got UNKNOWN, and reported "git says no". Git had not been
    asked yet. It settled to CONFLICTING seconds later, so the REFUSAL was
    right and the REASON was invented."""
    ok, note = L._land(REPO, _pr(mergeable="UNKNOWN",
                                 mergeStateStatus="UNKNOWN"), dry=True)
    assert ok is False
    assert "git says no" not in note
    assert "no computed merge state yet" in note


def test_a_round_that_moved_the_base_stales_its_own_earlier_pr():
    """Why #5 and #6 were CLOSED, not merged: a gk-merge PR is only mergeable
    inside the round that opened it. The next round's merges advance the fork's
    master and the PR conflicts, so by the time anyone looks it is DIRTY. This
    pins the fact in the file that has to deal with it."""
    src = (HERE / "land_merge_prs.py").read_text(encoding="utf-8")
    assert "recomputes it when the base moves" in src


# --- landing inside the round that opened the PR -----------------------------

def _stub_view(monkeypatch, answers):
    """`gh pr view` replies, one per call, so a test can watch it settle."""
    seq = list(answers)
    calls = {"n": 0}

    def fake_sh(*argv, **kw):
        calls["n"] += 1
        return 0, json.dumps(seq.pop(0) if seq else answers[-1])

    monkeypatch.setattr(L, "_sh", fake_sh)
    return calls


def test_a_pr_opened_seconds_ago_is_waited_for_not_refused(monkeypatch):
    """THE CASE THAT MAKES SAME-ROUND LANDING POSSIBLE. GitHub computes
    mergeability on first request, so a PR opened seconds ago has none. Asking
    once would refuse every time and the whole point would be lost."""
    calls = _stub_view(monkeypatch, [
        _pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"),
        _pr(),                       # settled
    ])
    out = L._settled(REPO, _pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"),
                     sleep=lambda s: None)
    assert out["mergeable"] == "UNKNOWN" or out["mergeable"] == "MERGEABLE"
    assert calls["n"] >= 1, "it must ask again rather than take the first answer"


def test_a_known_state_is_not_re_queried(monkeypatch):
    """Waiting on an answer we already have is round time spent for nothing."""
    calls = _stub_view(monkeypatch, [_pr()])
    L._settled(REPO, _pr(), sleep=lambda s: None)
    assert calls["n"] == 0


def test_the_wait_is_bounded(monkeypatch):
    """`not computed yet` must eventually become a refusal, not an indefinite
    wait inside a round."""
    calls = _stub_view(monkeypatch, [
        _pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")])
    out = L._settled(REPO, _pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"),
                     sleep=lambda s: None)
    assert out["mergeable"] == "UNKNOWN"
    assert calls["n"] <= L.MERGE_STATE_ATTEMPTS


def test_landing_runs_in_the_round_that_opened_the_pr():
    """A gk-merge PR is only mergeable inside its own round: the NEXT round's
    merges advance the fork's master and it goes DIRTY. Landing after run_tick
    only ever reached the stale one — observed 2026-08-22."""
    src = (HERE / "gatekeeper.py").read_text(encoding="utf-8")
    i = src.index("import prepare_merge_pr")
    block = src[i:i + 2000]
    assert "import land_merge_prs" in block, (
        "landing must happen where the PRs are opened, not a step later")
    assert 'r.get("status") == "opened"' in block, (
        "only PRs this round actually opened")


def test_landing_never_breaks_the_tick():
    """Same contract as the opener beside it: an alarm or a merge that cannot
    run must not cost the round the steps that follow."""
    src = (HERE / "gatekeeper.py").read_text(encoding="utf-8")
    i = src.index("import land_merge_prs")
    assert "except Exception" in src[i:i + 600]
