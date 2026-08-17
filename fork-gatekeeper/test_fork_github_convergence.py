#!/usr/bin/env python3
"""Hermetic proofs for the exact fork GitHub convergence collector."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "fork_github_convergence_under_test", HERE / "fork_github_convergence.py")
C = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = C
SPEC.loader.exec_module(C)


UPDATED_AT = "2026-08-17T05:10:00+00:00"
ROUND_AT = "2026-08-17T05:00:00+00:00"
RUN_ID = "a" * 64


def _node(kind: str, repo: str, number: int, *, title: str | None = None,
          **overrides):
    pr = kind == "pull_request"
    node = {
        "__typename": "PullRequest" if pr else "Issue",
        "number": number,
        "title": title or f"item {number}",
        "url": f"https://github.test/{repo}/{number}",
        "author": {"login": "vibeic"},
        "repository": {"nameWithOwner": repo},
        "updatedAt": UPDATED_AT,
    }
    if pr:
        node.update(headRefName=f"fix-{number}",
                    baseRefName="main", isDraft=False,
                    headRepositoryOwner={"login": "vibeic"})
    node.update(overrides)
    return node


def _noise_nodes(kind: str, start: int, count: int):
    return [
        _node(kind, f"elsewhere/noise-{number}", number)
        for number in range(start, start + count)
    ]


def _page(nodes, *, count=None, cursor=None, has_next=False,
          count_field="totalCount"):
    return {
        count_field: len(nodes) if count is None else count,
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


class ScriptedRunner:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, operation, query, variables):
        cursor = variables.get("cursor")
        self.calls.append((operation, cursor, dict(variables)))
        value = self.pages[(operation, cursor)]
        if isinstance(value, Exception):
            raise value
        return value


def _org(page):
    return {"data": {"search": page}}


def _viewer(connection: str, page, *, actor="reyerchu"):
    return {"data": {"viewer": {"login": actor, connection: page}}}


def _empty_pages():
    return {
        ("org_issues", None): _org(
            _page([], count_field="issueCount")),
        ("org_pull_requests", None): _org(
            _page([], count_field="issueCount")),
        ("viewer_issues", None): _viewer("issues", _page([])),
        ("viewer_pull_requests", None): _viewer("pullRequests", _page([])),
    }


def _measurement(tool: str, items=None, *, outcome="complete", **overrides):
    items = [] if items is None else items
    value = {
        "outcome": outcome,
        "upstream_repository": f"up/{tool}",
        "fork_owner": "vibeic",
        "open_count": len(items),
        "items": items,
    }
    value.update(overrides)
    return value


def _inputs(tmp_path: Path, *, tools=("alpha", "beta"), round_rows=None,
            round_meta=None):
    forks = tmp_path / "FORKS.json"
    forks.write_text(json.dumps({
        "org": "vibeic",
        "forks": [
            {"tool": tool, "upstream": f"up/{tool}"} for tool in tools
        ],
    }), encoding="utf-8")
    if round_rows is None:
        round_rows = {
            tool: {"upstream_pull_requests": _measurement(tool)}
            for tool in tools
        }
    if round_meta is None:
        round_meta = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "started_at": "2026-08-17T05:01:00+00:00",
            "finished_at": "2026-08-17T05:20:00+00:00",
        }
    payload = {"_round": round_meta, **round_rows}
    round_json = tmp_path / "round.json"
    round_json.write_text(json.dumps(payload), encoding="utf-8")
    return forks, round_json


NOW = lambda: datetime(2026, 8, 17, 5, 30, tzinfo=timezone.utc)


def test_exact_zero_requires_all_three_sources(tmp_path):
    forks, round_json = _inputs(tmp_path)
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 0
    assert record == {
        "schema_version": 1,
        "program": "fork_github_convergence",
        "started_at": "2026-08-17T05:30:00+00:00",
        "completed_at": "2026-08-17T05:30:00+00:00",
        "actor": "reyerchu",
        "expected_actor": "reyerchu",
        "round": {
            "round_at": ROUND_AT,
            "schema_version": 1,
            "run_id": RUN_ID,
            "started_at": "2026-08-17T05:01:00+00:00",
            "finished_at": "2026-08-17T05:20:00+00:00",
        },
        "status": "converged",
        "counts": {
            "items": 0, "issues": 0, "pull_requests": 0,
            "registered_fork_items": 0, "upstream_items": 0,
            "vibeic_eda_items": 0, "out_of_registry_org_items": 0,
            "by_source": {
                "org_search_issues": 0,
                "org_search_pull_requests": 0,
                "viewer_issues_matched": 0,
                "viewer_pull_requests_matched": 0,
                "round_upstream_pull_requests": 0,
            },
        },
        "items": [],
        "source_errors": [],
    }


def test_paginates_retains_out_of_registry_and_deduplicates_sources(tmp_path):
    forks, round_json = _inputs(tmp_path, round_rows={
        "alpha": {"upstream_pull_requests": _measurement("alpha", [
            {"number": 20, "title": "item 20",
             "url": "https://github.test/up/alpha/20",
             "head_ref": "fix-20", "base_ref": "main", "draft": False,
             "updated_at": UPDATED_AT},
        ])},
        "beta": {"upstream_pull_requests": _measurement("beta")},
    })
    viewer_pr_page_1 = [
        _node("pull_request", "vibeic/alpha", 2),
        _node("pull_request", "up/alpha", 20),
        *_noise_nodes("pull_request", 1000, 98),
    ]
    pages = {
        ("org_issues", None): _org(_page(
            [_node("issue", "vibeic/alpha", 1),
             _node("issue", "vibeic/not-registered", 9),
             _node("issue", "vibeic/vibeic-eda", 10)],
            count_field="issueCount")),
        ("org_pull_requests", None): _org(_page(
            [_node("pull_request", "vibeic/alpha", 2)],
            count_field="issueCount")),
        # This global viewer issue is deliberately outside the target fleet.
        ("viewer_issues", None): _viewer("issues", _page(
            [_node("issue", "someone/unrelated", 77)])),
        ("viewer_pull_requests", None): _viewer("pullRequests", _page(
            viewer_pr_page_1, count=101, cursor="viewer-pr-page-2",
            has_next=True)),
        ("viewer_pull_requests", "viewer-pr-page-2"): _viewer(
            "pullRequests", _page(
                _noise_nodes("pull_request", 2000, 1), count=101)),
    }
    runner = ScriptedRunner(pages)
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=runner, now=NOW)

    assert rc == 1 and record["status"] == "open"
    assert record["counts"]["items"] == 5
    assert record["counts"]["out_of_registry_org_items"] == 1
    assert record["counts"]["vibeic_eda_items"] == 1
    assert [(op, cursor) for op, cursor, _ in runner.calls
            if op == "viewer_pull_requests"] == [
                ("viewer_pull_requests", None),
                ("viewer_pull_requests", "viewer-pr-page-2")]

    fork_pr = next(x for x in record["items"]
                   if (x["repository"], x["number"]) == ("vibeic/alpha", 2))
    assert fork_pr["sources"] == ["org_search", "viewer"]
    upstream_pr = next(x for x in record["items"]
                       if (x["repository"], x["number"]) == ("up/alpha", 20))
    assert upstream_pr["sources"] == [
        "round_upstream_pull_requests", "viewer"]


@pytest.mark.parametrize("measurement", [
    None,
    _measurement("alpha", outcome="norecord"),
    {"outcome": "complete"},
])
def test_missing_or_incomplete_round_measurement_is_never_zero(tmp_path,
                                                               measurement):
    rows = {"alpha": {}}
    if measurement is not None:
        rows["alpha"]["upstream_pull_requests"] = measurement
    forks, round_json = _inputs(tmp_path, tools=("alpha",), round_rows=rows)
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert record["source_errors"]
    assert "upstream_pull_requests" in record["source_errors"][0]["source"]


@pytest.mark.parametrize("overrides,needle", [
    ({"upstream_repository": "wrong/repo"}, "upstream_repository"),
    ({"fork_owner": "someone-else"}, "fork_owner"),
    ({"fork_owner": "VIBEIC"}, "fork_owner"),
    ({"open_count": True}, "non-negative integer"),
    ({"open_count": -1}, "non-negative integer"),
    ({"open_count": 1}, "items has 0 row"),
    ({"items": None}, "items is not a list"),
])
def test_round_producer_envelope_must_prove_what_it_measured(
        tmp_path, overrides, needle):
    measurement = _measurement("alpha")
    measurement.update(overrides)
    forks, round_json = _inputs(
        tmp_path, tools=("alpha",),
        round_rows={"alpha": {"upstream_pull_requests": measurement}})
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    error = next(e["error"] for e in record["source_errors"]
                 if e["source"].endswith("upstream_pull_requests"))
    assert needle in error


def test_one_graphql_failure_makes_partial_open_evidence_norecord(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("org_issues", None)] = RuntimeError("network vanished")
    pages[("org_pull_requests", None)] = _org(_page(
        [_node("pull_request", "vibeic/alpha", 4)],
        count_field="issueCount"))
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert record["counts"]["items"] == 1  # known evidence is still retained
    assert any(e["source"] == "org_issues" for e in record["source_errors"])


@pytest.mark.parametrize("bad_page,needle", [
    ({"issueCount": 0, "nodes": []}, "pageInfo"),
    (_page([], count=1000, count_field="issueCount"), "1000-result cap"),
    (_page([], count=1, count_field="issueCount"), "fetched 0 nodes"),
    (_page([], count=0, cursor="p2", has_next=True,
           count_field="issueCount"), "issueCount=0 but hasNextPage"),
    (_page(_noise_nodes("issue", 1, 101), count=101,
           count_field="issueCount"), "maximum is 100"),
    (_page(_noise_nodes("issue", 1, 99), count=100, cursor="p2",
           has_next=True, count_field="issueCount"),
     "non-final page has 99 nodes"),
])
def test_malformed_or_capped_search_is_norecord(tmp_path, bad_page, needle):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("org_issues", None)] = _org(bad_page)
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    error = next(e["error"] for e in record["source_errors"]
                 if e["source"] == "org_issues")
    assert needle in error


def test_cursor_loop_is_detected_instead_of_hanging_or_claiming_zero(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("viewer_issues", None)] = _viewer("issues", _page(
        _noise_nodes("issue", 1, 100), count=300,
        cursor="again", has_next=True))
    pages[("viewer_issues", "again")] = _viewer("issues", _page(
        _noise_nodes("issue", 101, 100), count=300,
        cursor="again", has_next=True))
    record, rc = C.collect(forks, round_json,
                           round_at=ROUND_AT, expected_actor="reyerchu",
                           runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert "cursor loop" in next(e["error"] for e in record["source_errors"]
                                 if e["source"] == "viewer_issues")


def test_viewer_is_fully_paginated_even_when_every_item_is_filtered(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("viewer_pull_requests", None)] = _viewer("pullRequests", _page(
        _noise_nodes("pull_request", 1, 100), count=101,
        cursor="p2", has_next=True))
    pages[("viewer_pull_requests", "p2")] = _viewer("pullRequests", _page(
        _noise_nodes("pull_request", 101, 1), count=101))
    runner = ScriptedRunner(pages)
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=runner, now=NOW)
    assert rc == 0 and record["status"] == "converged"
    assert [(op, cursor) for op, cursor, _ in runner.calls
            if op == "viewer_pull_requests"] == [
                ("viewer_pull_requests", None),
                ("viewer_pull_requests", "p2"),
            ]


def test_duplicate_identity_across_pages_is_norecord_even_when_filtered(
        tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    first_page = _noise_nodes("issue", 1, 100)
    pages = _empty_pages()
    pages[("viewer_issues", None)] = _viewer("issues", _page(
        first_page, count=101, cursor="p2", has_next=True))
    pages[("viewer_issues", "p2")] = _viewer("issues", _page(
        [first_page[0]], count=101))
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    error = next(e["error"] for e in record["source_errors"]
                 if e["source"] == "viewer_issues")
    assert "duplicate item identity across pages" in error


def test_cli_writes_the_current_record_and_returns_status(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    out = tmp_path / "nested" / "convergence.json"
    rc = C.main([
        "--forks-json", str(forks),
        "--round-json", str(round_json),
        "--round-at", ROUND_AT,
        "--expected-actor", "reyerchu",
        "--json", str(out),
    ], runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 0
    record = json.loads(out.read_text())
    assert record["program"] == "fork_github_convergence"
    assert record["status"] == "converged"


def test_yesterdays_complete_zero_cannot_be_laundered_into_this_round(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",), round_meta={
        "schema_version": 1,
        "run_id": "b" * 64,
        "started_at": "2026-08-16T05:01:00+00:00",
        "finished_at": "2026-08-16T05:20:00+00:00",
    })
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    error = next(e["error"] for e in record["source_errors"]
                 if e["source"] == "round_json.provenance")
    assert "predates --round-at" in error
    assert record["round"]["run_id"] is None


@pytest.mark.parametrize("round_meta,needle", [
    ({}, "schema_version"),
    ({"schema_version": 1, "run_id": "short",
      "started_at": "2026-08-17T05:01:00+00:00",
      "finished_at": "2026-08-17T05:20:00+00:00"}, "run_id"),
    ({"schema_version": 1, "run_id": RUN_ID,
      "started_at": "2026-08-17T05:20:00+00:00",
      "finished_at": "2026-08-17T05:01:00+00:00"}, "predates started_at"),
    ({"schema_version": 1, "run_id": RUN_ID,
      "started_at": "2026-08-17T05:31:00+00:00",
      "finished_at": "2026-08-17T05:32:00+00:00"},
     "started_at is after collector started_at"),
    ({"schema_version": 1, "run_id": RUN_ID,
      "started_at": "2026-08-17T05:20:00+00:00",
      "finished_at": "2026-08-17T05:31:00+00:00"},
     "finished_at is after collector started_at"),
])
def test_malformed_round_provenance_is_norecord(tmp_path, round_meta, needle):
    forks, round_json = _inputs(
        tmp_path, tools=("alpha",), round_meta=round_meta)
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert needle in next(e["error"] for e in record["source_errors"]
                          if e["source"] == "round_json.provenance")


def test_wrong_authenticated_viewer_is_norecord_not_zero(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("viewer_issues", None)] = _viewer(
        "issues", _page([]), actor="wrong-account")
    pages[("viewer_pull_requests", None)] = _viewer(
        "pullRequests", _page([]), actor="wrong-account")
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert record["actor"] == "wrong-account"
    assert any("is not expected actor" in e["error"]
               for e in record["source_errors"])


@pytest.mark.parametrize("expected_actor", [None, "", " reyerchu", "reyerchu "])
def test_expected_actor_is_mandatory_even_for_direct_collection(
        tmp_path, expected_actor):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor=expected_actor,
        runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert any(e["source"] == "viewer.identity"
               and "non-empty exact login" in e["error"]
               for e in record["source_errors"])


def test_cli_without_expected_actor_writes_norecord(
        tmp_path, monkeypatch):
    monkeypatch.delenv("GK_GITHUB_ACTOR", raising=False)
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    out = tmp_path / "convergence.json"
    rc = C.main([
        "--forks-json", str(forks),
        "--round-json", str(round_json),
        "--round-at", ROUND_AT,
        "--json", str(out),
    ], runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2
    record = json.loads(out.read_text())
    assert record["status"] == "norecord"
    assert any(e["source"] == "viewer.identity"
               for e in record["source_errors"])


def test_viewer_identity_must_remain_stable_across_pages(tmp_path):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("viewer_issues", None)] = _viewer(
        "issues", _page(_noise_nodes("issue", 1, 100), count=101,
                        cursor="p2", has_next=True))
    pages[("viewer_issues", "p2")] = _viewer(
        "issues", _page(_noise_nodes("issue", 101, 1), count=101),
        actor="other-viewer")
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert "authenticated viewer changed" in next(
        e["error"] for e in record["source_errors"]
        if e["source"] == "viewer_issues")


@pytest.mark.parametrize("override,field", [
    ({"title": "different title"}, "title"),
    ({"url": "https://github.test/different"}, "url"),
    ({"headRefName": "different-head"}, "head_ref"),
    ({"baseRefName": "different-base"}, "base_ref"),
    ({"isDraft": True}, "draft"),
    ({"updatedAt": "2026-08-17T05:11:00+00:00"}, "updated_at"),
])
def test_conflicting_duplicate_details_are_norecord(tmp_path, override, field):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    pages = _empty_pages()
    pages[("org_pull_requests", None)] = _org(_page(
        [_node("pull_request", "vibeic/alpha", 7)],
        count_field="issueCount"))
    pages[("viewer_pull_requests", None)] = _viewer("pullRequests", _page([
        _node("pull_request", "vibeic/alpha", 7, **override),
    ]))
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(pages), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    error = next(e["error"] for e in record["source_errors"]
                 if e["source"] == "viewer_pull_requests")
    assert "conflicting duplicate" in error and field in error


@pytest.mark.parametrize("field,value", [
    ("head_ref", None),
    ("base_ref", ""),
    ("draft", 0),
    ("updated_at", "not-a-timestamp"),
])
def test_complete_round_pr_requires_actionable_fields(
        tmp_path, field, value):
    item = {
        "number": 3,
        "title": "upstream change",
        "url": "https://github.test/up/alpha/3",
        "head_ref": "fix-3",
        "base_ref": "main",
        "draft": False,
        "updated_at": UPDATED_AT,
    }
    item[field] = value
    forks, round_json = _inputs(tmp_path, tools=("alpha",), round_rows={
        "alpha": {"upstream_pull_requests": _measurement("alpha", [item])},
    })
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=ScriptedRunner(_empty_pages()), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert any(e["source"].endswith("upstream_pull_requests")
               for e in record["source_errors"])


def test_gh_deadline_timeout_forms_norecord_and_never_an_issue_verdict(
        tmp_path, monkeypatch):
    forks, round_json = _inputs(tmp_path, tools=("alpha",))
    seen_timeouts = []

    def timeout_run(*args, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(C.subprocess, "run", timeout_run)
    record, rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=C.GhGraphQLRunner(3.25), now=NOW)
    assert rc == 2 and record["status"] == "norecord"
    assert record["items"] == []
    assert seen_timeouts == [3.25, 3.25, 3.25, 3.25]
    assert all("formed no answer" in e["error"]
               for e in record["source_errors"]
               if e["source"].startswith(("org_", "viewer_")))
