#!/usr/bin/env python3
"""Hermetic proofs for exact, search-index-independent fork GitHub evidence."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fork_github_convergence as C  # noqa: E402


ROUND_AT = "2026-08-17T05:00:00+00:00"
UPDATED_AT = "2026-08-17T05:10:00+00:00"
RUN_ID = "a" * 64
NOW = lambda: datetime(2026, 8, 17, 5, 30, tzinfo=timezone.utc)


def _issue(repo: str, number: int, *, title=None, **overrides):
    node = {
        "__typename": "Issue", "number": number,
        "title": title or f"issue {number}",
        "url": f"https://github.test/{repo}/issues/{number}",
        "updatedAt": UPDATED_AT, "author": {"login": "vibeic"},
        "repository": {"nameWithOwner": repo},
    }
    node.update(overrides)
    return node


def _pr(repo: str, number: int, *, head="fix", head_owner="vibeic",
        base="main", draft=False, title=None, **overrides):
    node = {
        "__typename": "PullRequest", "number": number,
        "title": title or f"PR {number}",
        "url": f"https://github.test/{repo}/pull/{number}",
        "updatedAt": UPDATED_AT, "author": {"login": "vibeic"},
        "repository": {"nameWithOwner": repo},
        "headRefName": head, "baseRefName": base, "isDraft": draft,
        "headRepositoryOwner": ({"login": head_owner}
                                if head_owner is not None else None),
    }
    node.update(overrides)
    return node


def _connection(nodes=(), *, count=None, has_next=False, cursor=None):
    nodes = list(nodes)
    return {
        "totalCount": len(nodes) if count is None else count,
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


def _repo(repo: str, *, issues=(), pulls=(), is_fork=True,
          archived=False, issues_enabled=True,
          issue_connection=None, pull_connection=None):
    return {
        "nameWithOwner": repo, "isFork": is_fork,
        "isArchived": archived, "hasIssuesEnabled": issues_enabled,
        "issues": issue_connection or _connection(issues),
        "pullRequests": pull_connection or _connection(pulls),
    }


def _catalog(repositories, *, actor="reyerchu", count=None,
             has_next=False, cursor=None):
    repositories = list(repositories)
    return {"data": {
        "viewer": {"login": actor},
        "organization": {
            "login": "vibeic",
            "repositories": {
                "totalCount": len(repositories) if count is None else count,
                "nodes": repositories,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        },
    }}


def _viewer(field: str, nodes=(), *, actor="reyerchu", **connection_kwargs):
    return {"data": {"viewer": {
        "login": actor, field: _connection(nodes, **connection_kwargs),
    }}}


def _repo_followup(repo: str, *, issues=None, pulls=None, actor="reyerchu"):
    body = {"nameWithOwner": repo}
    if issues is not None:
        body["issues"] = issues
    if pulls is not None:
        body["pullRequests"] = pulls
    return {"data": {"viewer": {"login": actor}, "repository": body}}


class ScriptedRunner:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.calls = []

    @staticmethod
    def key(operation, variables):
        return (operation, variables.get("cursor"),
                variables.get("issueCursor"),
                variables.get("pullRequestCursor"))

    def __call__(self, operation, _query, variables):
        self.calls.append((operation, dict(variables)))
        value = self.pages[self.key(operation, variables)]
        if isinstance(value, Exception):
            raise value
        return value


def _measurement(tool: str, items=(), *, outcome="complete", **overrides):
    items = list(items)
    value = {
        "outcome": outcome, "upstream_repository": f"up/{tool}",
        "fork_owner": "vibeic", "open_count": len(items), "items": items,
    }
    value.update(overrides)
    return value


def _round_pr(repo: str, number: int, **overrides):
    item = {
        "repository": repo, "type": "pull_request", "number": number,
        "title": f"PR {number}",
        "url": f"https://github.test/{repo}/pull/{number}",
        "author": "vibeic", "head_owner": "vibeic",
        "head_ref": "fix", "base_ref": "main", "draft": False,
        "updated_at": UPDATED_AT,
    }
    item.update(overrides)
    return item


def _inputs(tmp_path: Path, *, tools=("alpha", "beta"), measurements=None):
    forks = tmp_path / "FORKS.json"
    forks.write_text(json.dumps({
        "org": "vibeic", "forks": [
            {"tool": tool, "upstream": f"up/{tool}"} for tool in tools
        ],
    }), encoding="utf-8")
    measurements = measurements or {
        tool: _measurement(tool) for tool in tools
    }
    payload = {
        "_round": {
            "schema_version": 1, "run_id": RUN_ID,
            "started_at": "2026-08-17T05:01:00+00:00",
            "finished_at": "2026-08-17T05:20:00+00:00",
        },
        **{tool: {"upstream_pull_requests": measurements.get(tool)}
           for tool in tools},
    }
    round_json = tmp_path / "round.json"
    round_json.write_text(json.dumps(payload), encoding="utf-8")
    return forks, round_json


def _required_nodes(*, alpha_issues=(), alpha_pulls=(), delivery_pulls=(),
                    alpha_issue_connection=None, alpha_pull_connection=None,
                    beta_issues_enabled=False, beta_issues=()):
    return [
        _repo("vibeic/alpha", issues=alpha_issues, pulls=alpha_pulls,
              issue_connection=alpha_issue_connection,
              pull_connection=alpha_pull_connection),
        # Control for the production edge: issue-disabled + exact zero is a
        # typed complete answer, not a missing source.
        _repo("vibeic/beta", issues=beta_issues,
              issues_enabled=beta_issues_enabled),
        _repo("vibeic/vibeic-eda", is_fork=False),
        _repo("vibeic/vibe-ic", pulls=delivery_pulls, is_fork=False),
    ]


def _base_pages(nodes=None):
    nodes = _required_nodes() if nodes is None else nodes
    return {
        ("organization.repositories", None, None, None): _catalog(nodes),
        ("viewer_upstream.issues", None, None, None): _viewer("issues"),
        ("viewer_upstream.pull_requests", None, None, None):
            _viewer("pullRequests"),
    }


def _collect(tmp_path, *, pages=None, tools=("alpha", "beta"),
             measurements=None, expected_actor="reyerchu"):
    forks, round_json = _inputs(
        tmp_path, tools=tools, measurements=measurements)
    runner = ScriptedRunner(_base_pages() if pages is None else pages)
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT,
        expected_actor=expected_actor, runner=runner, now=NOW)
    return record, rc, runner


def test_exact_zero_uses_one_catalog_request_not_search_or_2x_repo_calls(tmp_path):
    record, rc, runner = _collect(tmp_path)

    assert rc == 0 and record["status"] == "converged"
    assert record["schema_version"] == 2
    assert [op for op, _ in runner.calls] == [
        "organization.repositories", "viewer_upstream.issues",
        "viewer_upstream.pull_requests",
    ]
    assert not any("search" in op for op, _ in runner.calls)
    inventory = record["inventory"]
    assert inventory["status"] == "complete"
    assert inventory["required_repositories"] == [
        "vibeic/alpha", "vibeic/beta", "vibeic/vibeic-eda"]
    assert inventory["required_repository_count"] == 3
    assert inventory["completed_repository_count"] == 3
    beta = next(row for row in inventory["repositories"]
                if row["repository"] == "vibeic/beta")
    assert beta["has_issues_enabled"] is False
    assert beta["issues"] == {"outcome": "disabled", "open_count": 0}
    assert record["delivery"]["status"] == "complete"
    assert record["delivery"]["issues"] == {
        "outcome": "excluded_no_durable_producer_marker"}
    assert record["round_upstream"]["completed_fork_count"] == 2
    assert record["source_errors"] == []


def test_exact_repo_connections_find_items_that_search_can_falsely_report_zero(
        tmp_path):
    nodes = _required_nodes(
        alpha_issues=[_issue("vibeic/alpha", 7)],
        alpha_pulls=[_pr("vibeic/alpha", 8)])
    record, rc, _runner = _collect(tmp_path, pages=_base_pages(nodes))

    assert rc == 1 and record["status"] == "open"
    assert {(item["repository"], item["type"], item["number"])
            for item in record["items"]} == {
                ("vibeic/alpha", "issue", 7),
                ("vibeic/alpha", "pull_request", 8),
            }
    assert record["counts"]["registered_fork_items"] == 2
    assert record["counts"]["by_source"]["exact_repository_issues"] == 1
    assert record["counts"]["by_source"][
        "exact_repository_pull_requests"] == 1


def test_only_nonfinal_repo_connection_gets_a_followup_request(tmp_path):
    first = [_issue("vibeic/alpha", number) for number in range(1, 101)]
    nodes = _required_nodes(alpha_issue_connection=_connection(
        first, count=101, has_next=True, cursor="issue-page-2"))
    pages = _base_pages(nodes)
    pages[("repository.vibeic/alpha.open_items", None,
           "issue-page-2", None)] = _repo_followup(
               "vibeic/alpha", issues=_connection(
                   [_issue("vibeic/alpha", 101)], count=101))

    record, rc, runner = _collect(tmp_path, pages=pages)

    assert rc == 1 and record["counts"]["issues"] == 101
    followup = next(variables for operation, variables in runner.calls
                    if operation == "repository.vibeic/alpha.open_items")
    assert followup["includeIssues"] is True
    assert followup["includePullRequests"] is False
    assert followup["issueCursor"] == "issue-page-2"


def test_repo_followup_partial_data_with_graphql_errors_is_norecord(tmp_path):
    first = [_issue("vibeic/alpha", number) for number in range(1, 101)]
    nodes = _required_nodes(alpha_issue_connection=_connection(
        first, count=101, has_next=True, cursor="issue-page-2"))
    pages = _base_pages(nodes)
    partial = _repo_followup(
        "vibeic/alpha", issues=_connection(
            [_issue("vibeic/alpha", 101)], count=101))
    partial["errors"] = [{
        "message": "partial backend failure",
        "path": ["repository", "issues"],
    }]
    pages[("repository.vibeic/alpha.open_items", None,
           "issue-page-2", None)] = partial

    record, rc, _ = _collect(tmp_path, pages=pages)

    assert rc == 2 and record["status"] == "norecord"
    assert record["inventory"]["status"] == "norecord"
    assert any(error["source"] == "repository.vibeic/alpha.open_items"
               and "GraphQL errors" in error["error"]
               for error in record["source_errors"])


def test_pull_request_connection_is_independently_fully_paginated(tmp_path):
    first = [_pr("vibeic/alpha", number) for number in range(1, 101)]
    nodes = _required_nodes(alpha_pull_connection=_connection(
        first, count=101, has_next=True, cursor="pr-page-2"))
    pages = _base_pages(nodes)
    pages[("repository.vibeic/alpha.open_items", None,
           None, "pr-page-2")] = _repo_followup(
               "vibeic/alpha", pulls=_connection(
                   [_pr("vibeic/alpha", 101)], count=101))

    record, rc, runner = _collect(tmp_path, pages=pages)

    assert rc == 1 and record["counts"]["pull_requests"] == 101
    followup = next(variables for operation, variables in runner.calls
                    if operation == "repository.vibeic/alpha.open_items")
    assert followup["includeIssues"] is False
    assert followup["includePullRequests"] is True
    assert followup["pullRequestCursor"] == "pr-page-2"


def test_catalog_is_fully_paginated_and_keeps_one_exact_denominator(tmp_path):
    nodes = _required_nodes() + [
        _repo(f"vibeic/nonfork-{number}", is_fork=False)
        for number in range(97)
    ]
    assert len(nodes) == 101
    pages = _base_pages()
    pages[("organization.repositories", None, None, None)] = _catalog(
        nodes[:100], count=101, has_next=True, cursor="catalog-page-2")
    pages[("organization.repositories", "catalog-page-2", None, None)] = \
        _catalog(nodes[100:], count=101)

    record, rc, runner = _collect(tmp_path, pages=pages)

    assert rc == 0
    assert record["inventory"]["catalog"]["total_repositories"] == 101
    assert [op for op, _ in runner.calls].count(
        "organization.repositories") == 2


@pytest.mark.parametrize("second_page", [
    pytest.param("duplicate", id="duplicate-repository"),
    pytest.param("count-drift", id="total-count-drift"),
    pytest.param("cursor-loop", id="cursor-loop"),
])
def test_catalog_pagination_corruption_is_norecord(tmp_path, second_page):
    nodes = _required_nodes() + [
        _repo(f"vibeic/nonfork-{number}", is_fork=False)
        for number in range(96)
    ]
    assert len(nodes) == 100
    pages = _base_pages()
    pages[("organization.repositories", None, None, None)] = _catalog(
        nodes, count=101, has_next=True, cursor="catalog-page-2")
    if second_page == "duplicate":
        tail = _catalog([nodes[0]], count=101)
    elif second_page == "count-drift":
        tail = _catalog([_repo("vibeic/nonfork-last", is_fork=False)],
                        count=102)
    else:
        tail = _catalog([_repo("vibeic/nonfork-last", is_fork=False)],
                        count=101, has_next=True, cursor="catalog-page-2")
    pages[("organization.repositories", "catalog-page-2", None, None)] = tail

    record, rc, _ = _collect(tmp_path, pages=pages)

    assert rc == 2 and record["status"] == "norecord"
    assert record["inventory"]["catalog"]["outcome"] == "norecord"


def test_required_source_failure_freezes_later_network_and_is_never_zero(tmp_path):
    nodes = _required_nodes(alpha_issue_connection={"totalCount": 0})
    pages = _base_pages(nodes)
    record, rc, runner = _collect(tmp_path, pages=pages)

    assert rc == 2 and record["status"] == "norecord"
    assert [op for op, _ in runner.calls] == ["organization.repositories"]
    assert record["inventory"]["completed_repository_count"] == 0
    assert record["delivery"]["status"] == "norecord"
    assert record["upstream_actor"]["status"] == "norecord"
    assert record["source_errors"]


def test_issue_disabled_zero_is_complete_but_disabled_nonzero_is_norecord(
        tmp_path):
    clean, clean_rc, _ = _collect(tmp_path)
    assert clean_rc == 0
    assert next(row for row in clean["inventory"]["repositories"]
                if row["repository"] == "vibeic/beta")["issues"][
                    "outcome"] == "disabled"

    nodes = _required_nodes(
        beta_issues_enabled=False,
        beta_issues=[_issue("vibeic/beta", 1)])
    bad, bad_rc, _ = _collect(tmp_path, pages=_base_pages(nodes))
    assert bad_rc == 2 and bad["status"] == "norecord"
    assert any("hasIssuesEnabled=false" in error["error"]
               for error in bad["source_errors"])


def test_nonfork_canonical_watch_mirror_is_accepted(tmp_path):
    nodes = _required_nodes()
    nodes[1]["isFork"] = False  # the icsprout55-style watch-only mirror shape
    record, rc, _ = _collect(tmp_path, pages=_base_pages(nodes))
    assert rc == 0
    beta = next(row for row in record["inventory"]["repositories"]
                if row["repository"] == "vibeic/beta")
    assert beta["is_fork"] is False and beta["status"] == "complete"


def test_catalog_discovers_unregistered_live_fork_and_blocks_zero(tmp_path):
    nodes = _required_nodes() + [_repo("vibeic/forgotten")]
    record, rc, _ = _collect(tmp_path, pages=_base_pages(nodes))
    assert rc == 2 and record["inventory"]["status"] == "norecord"
    assert record["inventory"]["catalog"][
        "unregistered_live_fork_repositories"] == ["vibeic/forgotten"]
    assert any("absent from FORKS.json" in error["error"]
               for error in record["source_errors"])


def test_delivery_scope_uses_exact_producer_identity_and_retains_exclusions(
        tmp_path):
    delivery_prs = [
        _pr("vibeic/vibe-ic", 1, head="eda-anchor-0.3.5",
            title="wrong title", base="wrong", draft=True),
        _pr("vibeic/vibe-ic", 2, head="eda-fork-sync-2026-08-17",
            title="[eda-fork] 2026-08-17: sync"),
        _pr("vibeic/vibe-ic", 3, head="eda-fork-assess-2026-08-17",
            title=("[eda-fork] 2026-08-17: upstream release assessment — "
                   "1 tool(s) to review")),
        _pr("vibeic/vibe-ic", 4, head="eda-fork-random"),
        _pr("vibeic/vibe-ic", 5, head="eda-anchor-0.3.6",
            head_owner="someone"),
        _pr("vibeic/vibe-ic", 6, head="codex/unrelated"),
    ]
    record, rc, _ = _collect(
        tmp_path, pages=_base_pages(_required_nodes(
            delivery_pulls=delivery_prs)))

    assert rc == 1 and record["status"] == "open"
    delivery = record["delivery"]["pull_requests"]
    assert (delivery["open_count"], delivery["matched_count"],
            delivery["excluded_count"]) == (6, 3, 3)
    assert delivery["matched_count"] + delivery["excluded_count"] == \
        delivery["open_count"]
    assert {row["number"] for row in delivery["excluded"]} == {4, 5, 6}
    included = [item for item in record["items"]
                if "fork_delivery" in item["scopes"]]
    assert {item["delivery_kind"] for item in included} == {
        "anchor", "sync", "assessment"}
    anchor = next(item for item in included if item["delivery_kind"] == "anchor")
    assert set(anchor["delivery_anomalies"]) == {
        "base_ref_is_not_main", "draft_is_not_false",
        "title_does_not_match_anchor_producer",
    }
    assert record["counts"]["fork_delivery_items"] == 3


def test_unrelated_vibe_ic_backlog_does_not_gate_fork_convergence(tmp_path):
    nodes = _required_nodes(delivery_pulls=[
        _pr("vibeic/vibe-ic", 99, head="codex/plugin-work"),
    ])
    record, rc, _ = _collect(tmp_path, pages=_base_pages(nodes))

    assert rc == 0 and record["status"] == "converged"
    delivery = record["delivery"]["pull_requests"]
    assert delivery["open_count"] == 1
    assert delivery["matched_count"] == 0
    assert delivery["excluded_count"] == 1
    assert record["counts"]["fork_delivery_items"] == 0


def test_viewer_upstream_inventory_is_preserved_and_filters_unrelated(tmp_path):
    """PRESERVED and FILTERED — both halves, unchanged in subject.

    What moved on 2026-09-22 is only WHICH list preserves the issue: an issue
    we filed upstream is a report already delivered, so it is retained in
    `upstream_issue_items` instead of `items`. The upstream PR still gates, and
    `someone/unrelated` must still appear in neither list.
    """
    pages = _base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = _viewer("issues", [
        _issue("up/alpha", 20), _issue("someone/unrelated", 21),
    ])
    pages[("viewer_upstream.pull_requests", None, None, None)] = _viewer(
        "pullRequests", [_pr("up/beta", 22),
                         _pr("someone/unrelated", 23)])
    record, rc, _ = _collect(tmp_path, pages=pages)

    # The upstream PULL REQUEST is still owed work, so the round is still open.
    assert rc == 1
    assert {(item["repository"], item["number"])
            for item in record["items"]} == {("up/beta", 22)}
    assert {(item["repository"], item["number"])
            for item in record["upstream_issue_items"]} == {("up/alpha", 20)}
    retained = record["items"] + record["upstream_issue_items"]
    assert {(item["repository"], item["number"]) for item in retained} == {
        ("up/alpha", 20), ("up/beta", 22)}, "the inventory must stay whole"
    assert not any(item["repository"] == "someone/unrelated"
                   for item in retained), "unrelated repositories are filtered"
    assert record["upstream_actor"]["issues"] == {
        "outcome": "complete", "seen_count": 2, "matched_count": 1}
    assert record["counts"]["upstream_items"] == 1
    assert record["counts"]["upstream_issue_items_excluded"] == 1


def test_an_upstream_issue_alone_does_not_hold_the_round_open(tmp_path):
    """The other half of the split, and the whole point of it: with the PR
    removed, the same fixture converges. Before 2026-09-22 this exact shape was
    rc 1 — which is how OpenROAD#11471 skipped `build_page` for four rounds."""
    pages = _base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = _viewer("issues", [
        _issue("up/alpha", 20), _issue("someone/unrelated", 21),
    ])
    record, rc, _ = _collect(tmp_path, pages=pages)

    assert rc == 0 and record["status"] == "converged"
    assert record["items"] == []
    assert [(item["repository"], item["number"])
            for item in record["upstream_issue_items"]] == [("up/alpha", 20)]
    assert record["counts"]["upstream_issue_items_excluded"] == 1
    assert record["counts"]["upstream_items"] == 0


def test_viewer_is_fully_paginated_even_when_first_page_is_all_filtered(
        tmp_path):
    first = [_issue("someone/unrelated", number)
             for number in range(1, 101)]
    pages = _base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = _viewer(
        "issues", first, count=101, has_next=True, cursor="viewer-page-2")
    pages[("viewer_upstream.issues", "viewer-page-2", None, None)] = \
        _viewer("issues", [_issue("up/alpha", 101)], count=101)

    record, rc, runner = _collect(tmp_path, pages=pages)

    # The single match is an upstream ISSUE, reported rather than owed since
    # 2026-09-22, so the rc is 0 and the retained row is in
    # `upstream_issue_items`. This test's subject is unchanged: page two WAS
    # fetched and its match DID survive the filter.
    assert rc == 0
    assert [(item["repository"], item["number"])
            for item in record["upstream_issue_items"]] == [("up/alpha", 101)]
    assert record["upstream_actor"]["issues"] == {
        "outcome": "complete", "seen_count": 101, "matched_count": 1}
    assert ("viewer_upstream.issues", "viewer-page-2", None, None) in [
        ScriptedRunner.key(operation, variables)
        for operation, variables in runner.calls]


@pytest.mark.parametrize("fault", [
    pytest.param("duplicate", id="duplicate-across-pages"),
    pytest.param("count-drift", id="count-drift"),
    pytest.param("cursor-loop", id="cursor-loop"),
])
def test_viewer_pagination_corruption_is_norecord(tmp_path, fault):
    first = [_issue("someone/unrelated", number)
             for number in range(1, 101)]
    pages = _base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = _viewer(
        "issues", first, count=101, has_next=True, cursor="viewer-page-2")
    if fault == "duplicate":
        tail = _viewer("issues", [first[0]], count=101)
    elif fault == "count-drift":
        tail = _viewer("issues", [_issue("up/alpha", 101)], count=102)
    else:
        tail = _viewer("issues", [_issue("up/alpha", 101)], count=101,
                       has_next=True, cursor="viewer-page-2")
    pages[("viewer_upstream.issues", "viewer-page-2", None, None)] = tail

    record, rc, _ = _collect(tmp_path, pages=pages)

    assert rc == 2 and record["status"] == "norecord"
    assert record["upstream_actor"]["status"] == "norecord"


def test_viewer_failure_is_norecord_and_second_long_request_is_not_started(
        tmp_path):
    pages = _base_pages()
    pages[("viewer_upstream.issues", None, None, None)] = RuntimeError(
        "transport vanished")
    record, rc, runner = _collect(tmp_path, pages=pages)

    assert rc == 2 and record["upstream_actor"]["status"] == "norecord"
    assert "viewer_upstream.pull_requests" not in [op for op, _ in runner.calls]


def test_round_upstream_source_is_explicit_and_missing_row_is_norecord(tmp_path):
    measurements = {"alpha": _measurement("alpha"), "beta": None}
    record, rc, _ = _collect(tmp_path, measurements=measurements)

    assert rc == 2
    assert record["round_upstream"]["required_fork_count"] == 2
    assert record["round_upstream"]["completed_fork_count"] == 1
    beta = next(row for row in record["round_upstream"]["forks"]
                if row["tool"] == "beta")
    assert beta["outcome"] == "norecord" and beta["open_count"] is None


@pytest.mark.parametrize(("field", "value"), [
    pytest.param("upstream_repository", "wrong/repository", id="wrong-repo"),
    pytest.param("fork_owner", "someone", id="wrong-fork-owner"),
    pytest.param("open_count", 1, id="count-list-mismatch"),
    pytest.param("items", "not-a-list", id="items-not-list"),
    pytest.param("outcome", "norecord", id="incomplete-outcome"),
])
def test_round_producer_envelope_cannot_launder_zero(
        tmp_path, field, value):
    alpha = _measurement("alpha")
    alpha[field] = value
    record, rc, _ = _collect(tmp_path, measurements={
        "alpha": alpha, "beta": _measurement("beta")})

    assert rc == 2 and record["status"] == "norecord"
    row = next(row for row in record["round_upstream"]["forks"]
               if row["tool"] == "alpha")
    assert row["outcome"] == "norecord"


@pytest.mark.parametrize(("field", "value"), [
    pytest.param("title", None, id="missing-title"),
    pytest.param("url", None, id="missing-url"),
    pytest.param("head_ref", None, id="missing-head-ref"),
    pytest.param("base_ref", None, id="missing-base-ref"),
    pytest.param("draft", None, id="missing-draft"),
    pytest.param("updated_at", None, id="missing-updated-at"),
])
def test_round_pr_requires_actionable_normalized_fields(tmp_path, field, value):
    item = _round_pr("up/alpha", 1)
    item[field] = value
    record, rc, _ = _collect(tmp_path, measurements={
        "alpha": _measurement("alpha", [item]),
        "beta": _measurement("beta"),
    })
    assert rc == 2 and record["status"] == "norecord"


def test_conflicting_viewer_and_round_duplicate_is_norecord(tmp_path):
    pages = _base_pages()
    pages[("viewer_upstream.pull_requests", None, None, None)] = _viewer(
        "pullRequests", [_pr("up/alpha", 1, title="viewer title")])
    record, rc, _ = _collect(tmp_path, pages=pages, measurements={
        "alpha": _measurement("alpha", [
            _round_pr("up/alpha", 1, title="round title")]),
        "beta": _measurement("beta"),
    })
    assert rc == 2 and record["status"] == "norecord"
    assert any("conflicting duplicate" in error["error"]
               for error in record["source_errors"])


@pytest.mark.parametrize(("field", "value"), [
    pytest.param("run_id", "short", id="weak-run-id"),
    pytest.param("started_at", "2026-08-17T04:59:59+00:00",
                 id="prior-round"),
    pytest.param("finished_at", "2026-08-17T05:31:00+00:00",
                 id="future-finish"),
    pytest.param("finished_at", "2026-08-17T05:00:30+00:00",
                 id="finish-before-start"),
])
def test_malformed_round_provenance_is_norecord(tmp_path, field, value):
    forks, round_json = _inputs(tmp_path)
    payload = json.loads(round_json.read_text(encoding="utf-8"))
    payload["_round"][field] = value
    round_json.write_text(json.dumps(payload), encoding="utf-8")
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(_base_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert any(error["source"] == "round_json.provenance"
               for error in record["source_errors"])


def test_wrong_authenticated_actor_is_norecord_not_zero(tmp_path):
    nodes = _required_nodes()
    pages = _base_pages(nodes)
    pages[("organization.repositories", None, None, None)] = _catalog(
        nodes, actor="wrong")
    record, rc, _ = _collect(tmp_path, pages=pages)
    assert rc == 2 and record["actor"] == "wrong"
    assert any("expected actor" in error["error"]
               for error in record["source_errors"])


@pytest.mark.parametrize("expected_actor", [None, "", " reyerchu "])
def test_expected_actor_is_mandatory_for_direct_collection(
        tmp_path, expected_actor):
    record, rc, _ = _collect(tmp_path, expected_actor=expected_actor)
    assert rc == 2 and record["status"] == "norecord"
    assert any(error["source"] == "viewer.identity"
               for error in record["source_errors"])


def test_malformed_catalog_page_is_norecord_not_empty_inventory(tmp_path):
    nodes = _required_nodes()
    answer = _catalog(nodes)
    del answer["data"]["organization"]["repositories"]["pageInfo"]
    record, rc, _ = _collect(tmp_path, pages={
        ("organization.repositories", None, None, None): answer,
    })
    assert rc == 2
    assert record["inventory"]["catalog"]["outcome"] == "norecord"
    assert record["inventory"]["completed_repository_count"] == 0


@pytest.mark.parametrize("graphql_errors", [
    pytest.param([{
        "message": "partial backend failure",
        "path": ["organization", "repositories"],
    }], id="partial-data-with-errors"),
    pytest.param({"message": "not-an-errors-list"},
                 id="malformed-errors-field"),
])
def test_catalog_graphql_errors_are_norecord_not_exact_zero(
        tmp_path, graphql_errors):
    answer = _catalog(_required_nodes())
    answer["errors"] = graphql_errors

    record, rc, _ = _collect(tmp_path, pages={
        ("organization.repositories", None, None, None): answer,
    })

    assert rc == 2 and record["status"] == "norecord"
    assert record["inventory"]["catalog"]["outcome"] == "norecord"
    assert record["inventory"]["completed_repository_count"] == 0
    assert any(error["source"] == "organization.repositories"
               and "GraphQL" in error["error"]
               for error in record["source_errors"])


def test_transport_deadline_is_typed_no_answer(monkeypatch):
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gh", 3)

    monkeypatch.setattr(C.subprocess, "run", timed_out)
    runner = C.GhGraphQLRunner(3)
    with pytest.raises(C.SourceFailure, match="formed no answer"):
        runner("organization.repositories", "query", {})


def test_main_writes_v2_json_atomically(tmp_path):
    forks, round_json = _inputs(tmp_path)
    runner = ScriptedRunner(_base_pages())
    out = tmp_path / "convergence.json"
    rc = C.main([
        "--forks-json", str(forks), "--round-json", str(round_json),
        "--round-at", ROUND_AT, "--expected-actor", "reyerchu",
        "--json", str(out),
    ], runner=runner, now=NOW)
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0 and record["schema_version"] == 2
    assert not list(tmp_path.glob(".convergence.json.*"))


def test_cli_without_expected_actor_writes_norecord(tmp_path, monkeypatch):
    monkeypatch.delenv("GK_GITHUB_ACTOR", raising=False)
    forks, round_json = _inputs(tmp_path)
    out = tmp_path / "convergence.json"
    rc = C.main([
        "--forks-json", str(forks), "--round-json", str(round_json),
        "--round-at", ROUND_AT, "--json", str(out),
    ], runner=ScriptedRunner(_base_pages()), now=NOW)
    record = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 2 and record["status"] == "norecord"
    assert record["expected_actor"] is None


def test_source_contains_no_search_index_authority():
    source = (HERE / "fork_github_convergence.py").read_text(encoding="utf-8")
    assert "ORG_SEARCH_QUERY" not in source
    assert "org_search_issues" not in source
    assert "org_search_pull_requests" not in source
    assert "out_of_registry_org_items" not in source
    assert '"out_of_registry_org"' not in source
    assert "organization.repositories" in source
