#!/usr/bin/env python3
"""An issue we filed UPSTREAM is a report delivered, not a debt owed.

Same shape as `test_an_alarm_is_not_unfinished_work`, different trigger,
measured 2026-09-22. `The-OpenROAD-Project/OpenROAD#11471` — a bug this fleet
filed upstream at 05:40 +08:00, inside that morning's own 05:30 round — was the
SOLE item keeping `fork_github_convergence` non-zero. That skipped `build_page`
("current-round GitHub delivery is open or NORECORD"), so `eda-forks.html` sat
at its 2026-09-21 07:06 +08:00 publish for four consecutive rounds, and
`check_publish_silence` opened #223 about a silence the round itself caused.

The delivery happened when we pressed submit. What remains is a third party's
decision on a third party's clock: that repository had 94 open issues that day,
and of its last 100 closed the time-to-close was median 6.8 d, p90 54.9 d, max
130.6 d — #11471 had 0 comments, not yet triaged. Counting it hands the
publication date of our own public page to someone else's queue.

WHAT MUST NOT MOVE. An upstream PULL REQUEST is in-flight work and still gates.
It carries a branch we pushed and must keep alive; on 2026-07-31 an unguarded
`step4_prune` closed steveicarus/iverilog#1455 by deleting its head branch. So
the cut is on `type` and never on the `upstream` scope, and the tests below
prove the PR path with a CONSTRUCTED item rather than by reading the code.

EXCLUDED IS NOT HIDDEN. `upstream_issue_items_excluded`, the list
`upstream_issue_items`, and the summary line all name what was set aside.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import fork_github_convergence as F  # noqa: E402
import test_fork_github_convergence as T  # noqa: E402


def _issue_item(**over):
    item = {"repository": "The-OpenROAD-Project/OpenROAD", "type": "issue",
            "number": 11471, "head_owner": None, "head_ref": None,
            "author": "reyerchu", "scopes": ["upstream"],
            "sources": ["viewer_upstream"]}
    item.update(over)
    return item


def test_an_issue_we_filed_upstream_is_not_open_work():
    assert F._is_upstream_issue(_issue_item()) is True


def test_an_upstream_pull_request_is_still_open_work():
    """LOAD-BEARING. A PR carries a branch we own and can still lose; it is a
    delivery in flight, and the cut must not reach it."""
    assert F._is_upstream_issue(_issue_item(
        type="pull_request", number=1455,
        repository="steveicarus/iverilog",
        head_owner="vibeic", head_ref="fix/nb-event-trigger")) is False


@pytest.mark.parametrize("over", [
    # Our own repositories: every one of these is a delivery WE can finish.
    {"repository": "vibeic/vibeic-eda", "scopes": ["vibeic_eda"],
     "sources": ["exact_repository_issues"]},
    {"repository": "vibeic/yosys", "scopes": ["registered_fork"],
     "sources": ["exact_repository_issues"]},
    {"repository": "vibeic/vibe-ic", "scopes": ["fork_delivery"],
     "sources": ["fork_delivery_pull_requests"], "type": "pull_request"},
    # Right type, no upstream scope at all.
    {"scopes": []},
    {"scopes": None},
])
def test_our_own_work_is_still_counted(over):
    """An over-broad match here would let genuine unfinished work through."""
    assert F._is_upstream_issue(_issue_item(**over)) is False


def test_the_two_exclusions_are_orthogonal():
    """An alarm is matched by repository+branch, an upstream report by
    type+scope. Neither predicate may start answering for the other."""
    alarm = {"repository": F.VIBEIC_EDA_REPOSITORY, "type": "pull_request",
             "number": 223, "head_ref": "publish-silence-2026-09-22",
             "scopes": ["vibeic_eda"]}
    assert F._is_alarm(alarm) is True
    assert F._is_upstream_issue(alarm) is False
    assert F._is_alarm(_issue_item()) is False
    assert F._is_upstream_issue(_issue_item()) is True


def _eda_nodes(*, eda_pulls=(), alpha_issues=()):
    """`_required_nodes` cannot put a PR on vibeic-eda, which is where the
    alarm lives, so mirror it here."""
    return [
        T._repo("vibeic/alpha", issues=alpha_issues),
        T._repo("vibeic/beta", issues_enabled=False),
        T._repo("vibeic/vibeic-eda", pulls=list(eda_pulls), is_fork=False),
        T._repo("vibeic/vibe-ic", is_fork=False),
    ]


def test_end_to_end_an_upstream_issue_alone_converges(tmp_path):
    pages = T._base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = T._viewer(
        "issues", [T._issue("up/alpha", 20)])

    record, rc, _ = T._collect(tmp_path, pages=pages)

    assert rc == 0 and record["status"] == "converged"
    assert record["items"] == []
    assert [(i["repository"], i["number"])
            for i in record["upstream_issue_items"]] == [("up/alpha", 20)]
    assert record["counts"]["upstream_issue_items_excluded"] == 1


def test_end_to_end_an_upstream_pull_request_alone_still_blocks(tmp_path):
    """The constructed counter-case: change only the TYPE and the round is
    open again."""
    pages = T._base_pages()
    pages[("viewer_upstream.pull_requests", None, None, None)] = T._viewer(
        "pullRequests", [T._pr("up/alpha", 20)])

    record, rc, _ = T._collect(tmp_path, pages=pages)

    assert rc == 1 and record["status"] == "open"
    assert [(i["repository"], i["number"], i["type"]) for i in record["items"]] \
        == [("up/alpha", 20, "pull_request")]
    assert record["upstream_issue_items"] == []
    assert record["counts"]["upstream_items"] == 1
    assert record["counts"]["upstream_issue_items_excluded"] == 0


def test_end_to_end_an_alarm_is_still_excluded_and_not_mislabelled(tmp_path):
    pages = T._base_pages(_eda_nodes(eda_pulls=[
        T._pr("vibeic/vibeic-eda", 223, head="publish-silence-2026-09-22")]))

    record, rc, _ = T._collect(tmp_path, pages=pages)

    assert rc == 0 and record["status"] == "converged"
    assert [i["number"] for i in record["alarm_items"]] == [223]
    assert record["counts"]["alarm_items_excluded"] == 1
    assert record["upstream_issue_items"] == []
    assert record["counts"]["upstream_issue_items_excluded"] == 0


def test_end_to_end_our_own_repository_issue_still_blocks(tmp_path):
    pages = T._base_pages(_eda_nodes(
        alpha_issues=[T._issue("vibeic/alpha", 7)]))

    record, rc, _ = T._collect(tmp_path, pages=pages)

    assert rc == 1 and record["status"] == "open"
    assert [(i["repository"], i["number"]) for i in record["items"]] == [
        ("vibeic/alpha", 7)]
    assert record["upstream_issue_items"] == []


def test_end_to_end_an_ordinary_vibeic_eda_pr_still_blocks(tmp_path):
    """The alarm exclusion is a branch-prefix match; ordinary work on the same
    repository is untouched, and so is it by this second exclusion."""
    pages = T._base_pages(_eda_nodes(eda_pulls=[
        T._pr("vibeic/vibeic-eda", 224, head="fix/currency-venue")]))

    record, rc, _ = T._collect(tmp_path, pages=pages)

    assert rc == 1 and record["status"] == "open"
    assert [i["number"] for i in record["items"]] == [224]
    assert record["alarm_items"] == []
    assert record["upstream_issue_items"] == []


def test_the_summary_line_names_what_was_set_aside(tmp_path, capsys):
    """Run the real CLI. '0 open' must be distinguishable from 'converged, and
    one report was set aside'."""
    forks, round_json = T._inputs(tmp_path)
    pages = T._base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = T._viewer(
        "issues", [T._issue("up/alpha", 20)])
    out_json = tmp_path / "fork-github-convergence.json"

    rc = F.main(["--forks-json", str(forks), "--round-json", str(round_json),
                 "--round-at", T.ROUND_AT, "--expected-actor", "reyerchu",
                 "--json", str(out_json)],
                runner=T.ScriptedRunner(pages), now=T.NOW)

    assert rc == 0
    line = capsys.readouterr().out
    assert "converged — 0 open item(s)" in line
    assert "1 upstream issue(s) NOT counted as open work" in line
    assert "up/alpha#20" in line, "the repository must be named, not just #20"
    written = json.loads(out_json.read_text(encoding="utf-8"))
    assert written["counts"]["upstream_issue_items_excluded"] == 1
    assert written["upstream_issue_items"][0]["number"] == 20


def test_the_exclusion_is_reported_not_silent():
    """A filter nobody can see becomes a blind spot."""
    src = (_HERE / "fork_github_convergence.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert '"upstream_issue_items_excluded"' in code, "the count must reach the record"
    assert '"upstream_issue_items"' in code, "the excluded items themselves must too"
    assert "upstream issue(s) NOT counted as " in code, (
        "the human-readable summary must say what was set aside")


def test_the_consumer_reconciles_the_excluded_bucket_too():
    """`capture_release_ll` re-derives per-source counts from the retained
    rows. The alarm split failed there the round AFTER it shipped; this one
    must not repeat that, so the LL must add both buckets back."""
    ll = (_HERE / "capture_release_ll.py").read_text(encoding="utf-8")
    assert 'record.get("upstream_issue_items")' in ll, (
        "the excluded upstream reports must rejoin the source reconciliation")
    assert '"upstream_issue_items_excluded"}:' in ll, (
        "the exact schema-v2 counts key set must admit the new key")
