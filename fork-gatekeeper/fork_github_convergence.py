#!/usr/bin/env python3
"""Exact GitHub issue/PR convergence record for the fork fleet.

Zero is a strong statement here. It is emitted only after an authoritative,
paginated ``organization.repositories`` catalog proves the denominator; every
required repository's direct issue/PR connections are complete; the expected
actor's upstream connections are complete; and every fork's round-local
upstream-PR measurement is complete. GitHub's eventually-consistent search
index is deliberately not an authority. Any missing page, malformed cursor, or
absent source is ``norecord`` rather than an invented zero.

Each fork row's ``upstream_pull_requests`` envelope is load-bearing too.  It
must contain ``outcome``, ``upstream_repository``, ``fork_owner``,
``open_count``, and ``items``; the declared count must equal the list length.
This prevents a producer that queried the wrong repository (or never formed a
list) from laundering ``complete + []`` into convergence. The separate
``vibeic/vibe-ic`` delivery scope admits only producer-owned branch identities;
unrelated plugin PRs are retained as exclusions and never gate fork delivery.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 2
PROGRAM = "fork_github_convergence"
ORG = "vibeic"
RC_CONVERGED = 0
RC_OPEN = 1
RC_NORECORD = 2
DEFAULT_GH_REQUEST_DEADLINE_S = 120.0
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


ORG_REPOSITORIES_QUERY = r"""
query ForkConvergenceOrgRepositories($organization: String!, $cursor: String) {
  viewer {
    login
  }
  organization(login: $organization) {
    login
    repositories(first: 100, after: $cursor,
                 orderBy: {field: NAME, direction: ASC}) {
      totalCount
      nodes {
        nameWithOwner
        isFork
        isArchived
        hasIssuesEnabled
        issues(states: OPEN, first: 100) {
          totalCount
          nodes {
            __typename number title url updatedAt
            author { login }
            repository { nameWithOwner }
          }
          pageInfo { hasNextPage endCursor }
        }
        pullRequests(states: OPEN, first: 100) {
          totalCount
          nodes {
            __typename number title url headRefName baseRefName isDraft updatedAt
            author { login }
            headRepositoryOwner { login }
            repository { nameWithOwner }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPOSITORY_OPEN_ITEMS_QUERY = r"""
query ForkConvergenceRepositoryOpenItems(
  $owner: String!, $name: String!,
  $issueCursor: String, $pullRequestCursor: String,
  $includeIssues: Boolean!, $includePullRequests: Boolean!
) {
  viewer {
    login
  }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    issues(states: OPEN, first: 100, after: $issueCursor)
      @include(if: $includeIssues) {
      totalCount
      nodes {
        __typename number title url updatedAt
        author { login }
        repository { nameWithOwner }
      }
      pageInfo { hasNextPage endCursor }
    }
    pullRequests(states: OPEN, first: 100, after: $pullRequestCursor)
      @include(if: $includePullRequests) {
      totalCount
      nodes {
        __typename number title url headRefName baseRefName isDraft updatedAt
        author { login }
        headRepositoryOwner { login }
        repository { nameWithOwner }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

VIBEIC_EDA_REPOSITORY = "vibeic/vibeic-eda"

#: Branch prefix the publish-silence alarm files under, on this repo.
#:
#: AN ALARM IS NOT UNFINISHED WORK. Every other item here is a delivery the
#: fleet owes somebody; this one is the round telling a human that the public
#: page has stopped publishing. Counting it as open work made the alarm block
#: the very thing it reports on — measured 2026-08-21, where #138 ("fork page
#: has not published for 2 consecutive rounds") was the sole item keeping
#: convergence non-zero, which skipped build_page, which extended the silence
#: the PR exists to report.
#:
#: Matched on repository AND head_ref, not on title or author: a branch name
#: this program writes itself is the narrowest handle available in the PR
#: listing. Excluded items are still REPORTED — see `alarm_items` — so this
#: hides nothing, it only stops one kind of notice from being read as a debt.
# #82 — EVERY alarm prefix belongs here, not just the first one. A round opens
# these PRs to REPORT a problem; counting one as open work makes the alarm block
# the round it is reporting on, which is the failure #138 fixed for
# publish-silence. `release-failed-` is the second: a release that dies mid-build
# used to be visible only when publish-silence noticed the page had gone stale
# DAYS later, so the round now says so on the round it happened.
ALARM_BRANCH_PREFIXES = ("publish-silence-", "release-failed-")


def _is_alarm(item: dict) -> bool:
    return (item.get("repository") == VIBEIC_EDA_REPOSITORY
            and str(item.get("head_ref") or "").startswith(ALARM_BRANCH_PREFIXES))


#: AN ISSUE WE FILED UPSTREAM IS A REPORT ALREADY DELIVERED, NOT A DEBT.
#:
#: Same shape as the alarm above, different trigger, measured 2026-09-22.
#: `The-OpenROAD-Project/OpenROAD#11471` — a bug THIS fleet filed upstream at
#: 2026-09-22 05:40 +08:00, inside that morning's own 05:30 round.
#:
#: BE PRECISE ABOUT WHAT IT ACTUALLY COST, because the looser version of this
#: story is false and a comment nobody can reproduce is worth less than none.
#: Four rounds ran that day and the page published on none of them, but the
#: first three were skipped for their OWN reasons — semantic pytest RED at
#: 05:53, AI release authorization NORECORD at 13:38, release rc=5 at 15:19 —
#: and #223 was opened six seconds after the 13:38 skip, so this issue did not
#: cause that alarm and landing this does not clear it. At 13:29 the open set
#: was two items, not one (#11471 plus vibeic/vibeic-eda PR #222).
#:
#: The round that matters is the LAST one. At 17:38 the tick reached exit 0 and
#: released 0.3.72; everything else was finally green, and #11471 was then the
#: only item left holding this program non-zero, which skipped `build_page` (see
#: run_0530.sh: "current-round GitHub delivery is open or NORECORD") and left
#: `eda-forks.html` on its 2026-09-21 07:06 +08:00 publish. That is the whole
#: argument: not what it has already cost, but that from here it holds EVERY
#: future round, for as long as somebody else's queue takes.
#:
#: WHAT IT COSTS, measured the same day on that repository: 94 open issues; of
#: its last 100 closed, time-to-close median 6.8 d, p75 17.5 d, p90 54.9 d, max
#: 130.6 d, only 18% inside a day. #11471 had 0 comments — not yet triaged.
#: Counting it hands the publication date of OUR public page to SOMEONE ELSE'S
#: queue, for a delivery we already made the moment we pressed submit. Nothing
#: on our side remains to be done, so there is nothing for convergence to wait
#: on.
#:
#: AN UPSTREAM PULL REQUEST IS THE OPPOSITE AND STILL COUNTS. It carries
#: `head_owner`/`head_ref` — a branch we pushed and must keep alive — and this
#: program already measures it TWICE, once via `viewer_upstream` and again per
#: fork via `round.<tool>.upstream_pull_requests`. On 2026-07-31 an unguarded
#: `step4_prune` closed our only upstream PR (steveicarus/iverilog#1455) by
#: deleting its head branch; that is work this fleet can still lose, so it is
#: in flight and it gates. The cut is therefore on `type`, NEVER on the
#: `upstream` scope, which would take the PRs with it.
#:
#: Matched on type AND scope, the narrowest handle: `scopes` already carries
#: the semantics every other count in this record keys on, `upstream` is
#: disjoint from `registered_fork`/`vibeic_eda`/`fork_delivery` (no `vibeic/*`
#: repository is an upstream), and `author` is no discriminator at all —
#: `viewer.issues` returns only the viewer's own. Replayed over the 62 distinct
#: items these rounds have ever produced, it excludes exactly one: #11471.
#:
#: This branch had never executed before. 110 archived rounds, 897 issues
#: authored by this actor, exactly one of them in an upstream repository — the
#: one above. The four earlier rounds that ever carried an upstream item
#: (2026-08-17/18) carried iverilog#1455, a PR.
#:
#: Excluded items are still REPORTED — see `upstream_issue_items` — so this
#: hides nothing, exactly as `alarm_items` does for a notice.
def _is_upstream_issue(item: dict) -> bool:
    return (item.get("type") == "issue"
            and "upstream" in (item.get("scopes") or []))


DELIVERY_REPOSITORY = "vibeic/vibe-ic"
DELIVERY_PR_HEAD_PATTERNS = {
    "anchor": re.compile(r"^eda-anchor-(\d+\.\d+\.\d+)$"),
    "sync": re.compile(r"^eda-fork-sync-(\d{4}-\d{2}-\d{2})$"),
    "assessment": re.compile(r"^eda-fork-assess-(\d{4}-\d{2}-\d{2})$"),
}

VIEWER_ISSUES_QUERY = r"""
query ForkConvergenceViewerIssues($cursor: String) {
  viewer {
    login
    issues(states: OPEN, first: 100, after: $cursor) {
      totalCount
      nodes {
        __typename number title url updatedAt
        author { login }
        repository { nameWithOwner }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

VIEWER_PRS_QUERY = r"""
query ForkConvergenceViewerPullRequests($cursor: String) {
  viewer {
    login
    pullRequests(states: OPEN, first: 100, after: $cursor) {
      totalCount
      nodes {
        __typename number title url headRefName baseRefName isDraft updatedAt
        author { login }
        headRepositoryOwner { login }
        repository { nameWithOwner }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class SourceFailure(RuntimeError):
    """A source did not produce a complete, exact answer."""


class GhGraphQLRunner:
    """Authenticated GraphQL transport. Runtime is not used as a verdict."""

    def __init__(self, request_deadline_s: float = DEFAULT_GH_REQUEST_DEADLINE_S):
        if (isinstance(request_deadline_s, bool)
                or not isinstance(request_deadline_s, (int, float))
                or not math.isfinite(float(request_deadline_s))
                or float(request_deadline_s) <= 0):
            raise ValueError("request_deadline_s must be a positive finite number")
        self.request_deadline_s = float(request_deadline_s)

    def __call__(self, operation: str, query: str,
                 variables: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = json.dumps({"query": query, "variables": dict(variables)})
        try:
            cp = subprocess.run(
                ["gh", "api", "graphql", "--input", "-"],
                input=payload, capture_output=True, text=True,
                timeout=self.request_deadline_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceFailure(
                f"{operation}: gh request exceeded the operational "
                f"{self.request_deadline_s:g}s deadline; GitHub formed no "
                "answer, so no issue/PR verdict exists") from exc
        except OSError as exc:
            raise SourceFailure(
                f"{operation}: GraphQL transport did not start: "
                f"{type(exc).__name__}: {exc}") from exc
        if cp.returncode != 0:
            detail = " ".join((cp.stderr or cp.stdout or "").split())
            raise SourceFailure(
                f"{operation}: gh api graphql rc={cp.returncode}"
                + (f": {detail[:500]}" if detail else ""))
        try:
            answer = json.loads(cp.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SourceFailure(
                f"{operation}: GraphQL returned malformed JSON") from exc
        if not isinstance(answer, dict):
            raise SourceFailure(f"{operation}: GraphQL root is not an object")
        return answer


Runner = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class Registry:
    tools: tuple[str, ...]
    registered_repos: Mapping[str, str]
    upstream_by_tool: Mapping[str, str]
    upstream_repos: Mapping[str, str]


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceFailure(f"{label}: cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceFailure(f"{label}: malformed JSON in {path}: {exc}") from exc


def _valid_slug(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (len(parts) == 2 and all(parts)
            and all(part.strip() == part for part in parts)
            and not any(any(ch.isspace() for ch in part) for part in parts))


def load_registry(path: Path) -> Registry:
    raw = _read_json(path, "forks_json")
    if not isinstance(raw, dict) or not isinstance(raw.get("forks"), list):
        raise SourceFailure("forks_json: root must contain a forks list")
    org = raw.get("org", ORG)
    if not isinstance(org, str) or org.lower() != ORG:
        raise SourceFailure(f"forks_json: org must be {ORG!r}")

    tools: list[str] = []
    registered: dict[str, str] = {}
    upstream_by_tool: dict[str, str] = {}
    upstreams: dict[str, str] = {}
    for pos, row in enumerate(raw["forks"]):
        if not isinstance(row, dict):
            raise SourceFailure(f"forks_json: forks[{pos}] is not an object")
        tool = row.get("tool")
        upstream = row.get("upstream")
        if not isinstance(tool, str) or not tool.strip() or any(
                ch in tool for ch in "/\t\r\n"):
            raise SourceFailure(f"forks_json: forks[{pos}] has invalid tool")
        if not _valid_slug(upstream):
            raise SourceFailure(
                f"forks_json: {tool} has invalid upstream owner/repo")
        tkey = tool.lower()
        if tkey in {x.lower() for x in tools}:
            raise SourceFailure(f"forks_json: duplicate tool {tool}")
        fork_repo = f"{ORG}/{tool}"
        tools.append(tool)
        registered[fork_repo.lower()] = fork_repo
        upstream_by_tool[tool] = upstream
        upstreams.setdefault(upstream.lower(), upstream)
    if not tools:
        raise SourceFailure("forks_json: no registered forks")
    return Registry(tuple(tools), registered, upstream_by_tool, upstreams)


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SourceFailure(f"{context}: expected object")
    return value


def _graphql_root(response: Mapping[str, Any], operation: str
                  ) -> Mapping[str, Any]:
    """Reject every malformed or partial GraphQL answer before using data."""
    root = _mapping(response, operation)
    if "errors" in root:
        errors = root["errors"]
        if not isinstance(errors, list):
            raise SourceFailure(
                f"{operation}: malformed GraphQL errors field")
        if errors:
            raise SourceFailure(
                f"{operation}: GraphQL errors: "
                + " ".join(str(errors).split())[:500])
    return root


def _connection(response: Mapping[str, Any], path: Sequence[str],
                operation: str) -> Mapping[str, Any]:
    root = _graphql_root(response, operation)
    cur: Any = root.get("data")
    for name in path:
        cur = _mapping(cur, f"{operation}.data path").get(name)
    return _mapping(cur, f"{operation}.connection")


def paginate_connection(runner: Runner, *, operation: str, query: str,
                        variables: Mapping[str, Any], path: Sequence[str],
                        count_field: str,
                        page_validator: Callable[[Mapping[str, Any], str], None]
                        | None = None,
                        ) -> list[Mapping[str, Any]]:
    """Read an entire GraphQL connection or raise; partial is never complete."""
    cursor: str | None = None
    seen_cursors: set[str | None] = set()
    seen_item_identities: set[tuple[str, str, int]] = set()
    expected_count: int | None = None
    nodes: list[Mapping[str, Any]] = []

    while True:
        if cursor in seen_cursors:
            raise SourceFailure(f"{operation}: cursor loop at {cursor!r}")
        seen_cursors.add(cursor)
        page_vars = dict(variables)
        page_vars["cursor"] = cursor
        try:
            response = runner(operation, query, page_vars)
        except SourceFailure:
            raise
        except Exception as exc:  # injectable transports must fail as NORECORD
            raise SourceFailure(
                f"{operation}: runner failed: {type(exc).__name__}: {exc}") from exc
        if page_validator is not None:
            page_validator(response, operation)
        conn = _connection(response, path, operation)

        count = conn.get(count_field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SourceFailure(f"{operation}: malformed {count_field}")
        if expected_count is None:
            expected_count = count
        elif count != expected_count:
            raise SourceFailure(
                f"{operation}: {count_field} changed during pagination "
                f"({expected_count} -> {count})")

        page_nodes = conn.get("nodes")
        if not isinstance(page_nodes, list) or any(
                not isinstance(node, dict) for node in page_nodes):
            raise SourceFailure(f"{operation}: malformed nodes list")
        if len(page_nodes) > 100:
            raise SourceFailure(
                f"{operation}: page has {len(page_nodes)} nodes; maximum is 100")

        page_info = conn.get("pageInfo")
        if not isinstance(page_info, dict):
            raise SourceFailure(f"{operation}: missing/malformed pageInfo")
        has_next = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next, bool):
            raise SourceFailure(f"{operation}: malformed hasNextPage")
        if has_next:
            if count == 0:
                raise SourceFailure(
                    f"{operation}: {count_field}=0 but hasNextPage is true")
            if len(page_nodes) != 100:
                raise SourceFailure(
                    f"{operation}: non-final page has {len(page_nodes)} "
                    "nodes; expected exactly 100")
            if not isinstance(end_cursor, str) or not end_cursor:
                raise SourceFailure(
                    f"{operation}: hasNextPage without a usable endCursor")

        for node in page_nodes:
            typename = node.get("__typename")
            repo_obj = node.get("repository")
            repo = repo_obj.get("nameWithOwner") \
                if isinstance(repo_obj, dict) else None
            number = node.get("number")
            if (typename not in ("Issue", "PullRequest")
                    or not _valid_slug(repo)
                    or isinstance(number, bool) or not isinstance(number, int)
                    or number <= 0):
                raise SourceFailure(
                    f"{operation}: node lacks a stable item identity")
            identity = (repo.lower(), typename, number)
            if identity in seen_item_identities:
                raise SourceFailure(
                    f"{operation}: duplicate item identity across pages: "
                    f"{repo} {typename} #{number}")
            seen_item_identities.add(identity)
        nodes.extend(page_nodes)

        if has_next:
            cursor = end_cursor
            continue
        break

    assert expected_count is not None
    if len(nodes) != expected_count:
        raise SourceFailure(
            f"{operation}: fetched {len(nodes)} nodes but {count_field} is "
            f"{expected_count}")
    return nodes


@dataclass
class _OpenConnection:
    field: str
    typename: str
    count: int | None = None
    nodes: list[Mapping[str, Any]] | None = None
    cursor: str | None = None
    complete: bool = False
    seen_cursors: set[str | None] | None = None
    identities: set[tuple[str, str, int]] | None = None

    def __post_init__(self) -> None:
        self.nodes = []
        self.seen_cursors = {None}
        self.identities = set()


@dataclass(frozen=True)
class _CatalogRepository:
    repository: str
    is_fork: bool
    is_archived: bool
    has_issues_enabled: bool
    issues_page: Mapping[str, Any]
    pull_requests_page: Mapping[str, Any]


def _accept_open_page(state: _OpenConnection, value: Any, *,
                      operation: str, expected_repo: str) -> None:
    """Consume one exact repository connection page into mutable evidence."""
    conn = _mapping(value, f"{operation}.{state.field}")
    count = conn.get("totalCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise SourceFailure(f"{operation}.{state.field}: malformed totalCount")
    if state.count is None:
        state.count = count
    elif count != state.count:
        raise SourceFailure(
            f"{operation}.{state.field}: totalCount changed during pagination "
            f"({state.count} -> {count})")
    page_nodes = conn.get("nodes")
    if not isinstance(page_nodes, list) or any(
            not isinstance(node, dict) for node in page_nodes):
        raise SourceFailure(f"{operation}.{state.field}: malformed nodes list")
    if len(page_nodes) > 100:
        raise SourceFailure(
            f"{operation}.{state.field}: page has more than 100 nodes")
    page_info = conn.get("pageInfo")
    if not isinstance(page_info, dict):
        raise SourceFailure(
            f"{operation}.{state.field}: missing/malformed pageInfo")
    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next, bool):
        raise SourceFailure(
            f"{operation}.{state.field}: malformed hasNextPage")
    if has_next:
        if count == 0 or len(page_nodes) != 100:
            raise SourceFailure(
                f"{operation}.{state.field}: non-final page is not exactly 100")
        if not isinstance(end_cursor, str) or not end_cursor:
            raise SourceFailure(
                f"{operation}.{state.field}: no usable endCursor")
        assert state.seen_cursors is not None
        if end_cursor in state.seen_cursors:
            raise SourceFailure(
                f"{operation}.{state.field}: cursor loop at {end_cursor!r}")

    assert state.identities is not None and state.nodes is not None
    for node in page_nodes:
        repo = ((node.get("repository") or {}).get("nameWithOwner")
                if isinstance(node.get("repository"), dict) else None)
        number = node.get("number")
        if (node.get("__typename") != state.typename
                or not isinstance(repo, str)
                or repo.lower() != expected_repo.lower()
                or isinstance(number, bool) or not isinstance(number, int)
                or number <= 0):
            raise SourceFailure(
                f"{operation}.{state.field}: node lacks the exact repository "
                "item identity")
        identity = (repo.lower(), state.typename, number)
        if identity in state.identities:
            raise SourceFailure(
                f"{operation}.{state.field}: duplicate item {repo} #{number}")
        state.identities.add(identity)
    state.nodes.extend(page_nodes)
    state.complete = not has_next
    state.cursor = end_cursor if has_next else None
    if state.complete and len(state.nodes) != state.count:
        raise SourceFailure(
            f"{operation}.{state.field}: fetched {len(state.nodes)} nodes but "
            f"totalCount is {state.count}")


def _finish_repository_pages(
        runner: Runner, entry: _CatalogRepository,
        validate_response: Callable[[Mapping[str, Any], str, str], None],
        *, include_issues: bool = True,
        ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Finish both connections, querying only whichever one has another page."""
    operation = f"repository.{entry.repository}.open_items"
    issues = _OpenConnection("issues", "Issue")
    pulls = _OpenConnection("pullRequests", "PullRequest")
    if include_issues:
        _accept_open_page(issues, entry.issues_page, operation=operation,
                          expected_repo=entry.repository)
    else:
        issues.complete = True
        issues.count = 0
    _accept_open_page(pulls, entry.pull_requests_page, operation=operation,
                      expected_repo=entry.repository)
    if include_issues and not entry.has_issues_enabled and issues.count != 0:
        raise SourceFailure(
            f"{operation}.issues: hasIssuesEnabled=false but totalCount is "
            f"{issues.count}")
    while not (issues.complete and pulls.complete):
        issue_active = not issues.complete
        pull_active = not pulls.complete
        for state, active in ((issues, issue_active), (pulls, pull_active)):
            if active:
                assert state.cursor is not None and state.seen_cursors is not None
                if state.cursor in state.seen_cursors:
                    raise SourceFailure(
                        f"{operation}.{state.field}: cursor loop at "
                        f"{state.cursor!r}")
                state.seen_cursors.add(state.cursor)
        owner, name = entry.repository.split("/", 1)
        variables = {
            "owner": owner, "name": name,
            "issueCursor": issues.cursor if issue_active else None,
            "pullRequestCursor": pulls.cursor if pull_active else None,
            "includeIssues": issue_active,
            "includePullRequests": pull_active,
        }
        try:
            response = runner(operation, REPOSITORY_OPEN_ITEMS_QUERY, variables)
        except SourceFailure:
            raise
        except Exception as exc:
            raise SourceFailure(
                f"{operation}: runner failed: {type(exc).__name__}: {exc}") from exc
        validate_response(response, operation, entry.repository)
        repo = _mapping(_mapping(response, operation).get("data"),
                        f"{operation}.data").get("repository")
        repo_obj = _mapping(repo, f"{operation}.repository")
        if issue_active:
            _accept_open_page(issues, repo_obj.get("issues"),
                              operation=operation,
                              expected_repo=entry.repository)
        if pull_active:
            _accept_open_page(pulls, repo_obj.get("pullRequests"),
                              operation=operation,
                              expected_repo=entry.repository)
    assert issues.nodes is not None and pulls.nodes is not None
    return issues.nodes, pulls.nodes


def _catalog_repositories(
        runner: Runner,
        validate_response: Callable[[Mapping[str, Any], str], None],
        ) -> tuple[int, dict[str, _CatalogRepository]]:
    """Read the exact organization repository denominator and first item pages."""
    operation = "organization.repositories"
    cursor: str | None = None
    seen_cursors: set[str | None] = set()
    expected_count: int | None = None
    repositories: dict[str, _CatalogRepository] = {}
    while True:
        if cursor in seen_cursors:
            raise SourceFailure(f"{operation}: cursor loop at {cursor!r}")
        seen_cursors.add(cursor)
        try:
            response = runner(
                operation, ORG_REPOSITORIES_QUERY,
                {"organization": ORG, "cursor": cursor})
        except SourceFailure:
            raise
        except Exception as exc:
            raise SourceFailure(
                f"{operation}: runner failed: {type(exc).__name__}: {exc}") from exc
        validate_response(response, operation)
        data = _mapping(_mapping(response, operation).get("data"),
                        f"{operation}.data")
        organization = _mapping(data.get("organization"),
                                f"{operation}.organization")
        if organization.get("login") != ORG:
            raise SourceFailure(
                f"{operation}: organization login is not {ORG!r}")
        conn = _mapping(organization.get("repositories"),
                        f"{operation}.repositories")
        count = conn.get("totalCount")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SourceFailure(f"{operation}: malformed totalCount")
        if expected_count is None:
            expected_count = count
        elif count != expected_count:
            raise SourceFailure(
                f"{operation}: totalCount changed ({expected_count} -> {count})")
        nodes = conn.get("nodes")
        if not isinstance(nodes, list) or any(
                not isinstance(node, dict) for node in nodes):
            raise SourceFailure(f"{operation}: malformed nodes list")
        if len(nodes) > 100:
            raise SourceFailure(f"{operation}: page has more than 100 nodes")
        page_info = conn.get("pageInfo")
        if not isinstance(page_info, dict):
            raise SourceFailure(f"{operation}: missing/malformed pageInfo")
        has_next = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next, bool):
            raise SourceFailure(f"{operation}: malformed hasNextPage")
        if has_next and (len(nodes) != 100 or not isinstance(end_cursor, str)
                         or not end_cursor):
            raise SourceFailure(f"{operation}: malformed non-final page")
        for node in nodes:
            repo = node.get("nameWithOwner")
            is_fork, is_archived = node.get("isFork"), node.get("isArchived")
            has_issues = node.get("hasIssuesEnabled")
            if (not _valid_slug(repo)
                    or not repo.lower().startswith(f"{ORG}/")
                    or not isinstance(is_fork, bool)
                    or not isinstance(is_archived, bool)
                    or not isinstance(has_issues, bool)):
                raise SourceFailure(f"{operation}: malformed repository node")
            key = repo.lower()
            if key in repositories:
                raise SourceFailure(f"{operation}: duplicate repository {repo}")
            repositories[key] = _CatalogRepository(
                repo, is_fork, is_archived, has_issues,
                _mapping(node.get("issues"), f"{operation}.{repo}.issues"),
                _mapping(node.get("pullRequests"),
                         f"{operation}.{repo}.pullRequests"),
            )
        if not has_next:
            break
        cursor = end_cursor
    assert expected_count is not None
    if len(repositories) != expected_count:
        raise SourceFailure(
            f"{operation}: fetched {len(repositories)} repositories but "
            f"totalCount is {expected_count}")
    return expected_count, repositories


def _login(value: Any) -> str | None:
    return value.get("login") if isinstance(value, dict) \
        and isinstance(value.get("login"), str) else None


def _timestamp(value: Any, context: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise SourceFailure(f"{context}: expected a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceFailure(f"{context}: malformed timestamp {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceFailure(f"{context}: timestamp has no timezone")
    # Keep non-zero fractional precision from the producer/API.  The parsed
    # value is canonical enough for duplicate comparison without erasing
    # provenance that may distinguish adjacent measurements.
    return parsed, parsed.isoformat()


def _round_provenance(raw: Any, round_at: str,
                      collector_started_at: datetime) -> dict[str, Any]:
    root = _mapping(raw, "round_json")
    envelope = _mapping(root.get("_round"), "round_json._round")
    schema_version = envelope.get("schema_version")
    if (isinstance(schema_version, bool) or not isinstance(schema_version, int)
            or schema_version != 1):
        raise SourceFailure(
            "round_json._round.schema_version: expected integer 1")
    run_id = envelope.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise SourceFailure(
            "round_json._round.run_id: expected a 32-256 character "
            "cryptographic identifier")
    required_at, canonical_round_at = _timestamp(round_at, "round_at")
    started_at, canonical_started_at = _timestamp(
        envelope.get("started_at"), "round_json._round.started_at")
    finished_at, canonical_finished_at = _timestamp(
        envelope.get("finished_at"), "round_json._round.finished_at")
    if started_at < required_at:
        raise SourceFailure(
            "round_json._round.started_at predates --round-at; a prior round "
            "cannot prove this round")
    if finished_at < started_at:
        raise SourceFailure(
            "round_json._round.finished_at predates started_at")
    if started_at > collector_started_at:
        raise SourceFailure(
            "round_json._round.started_at is after collector started_at")
    if finished_at > collector_started_at:
        raise SourceFailure(
            "round_json._round.finished_at is after collector started_at")
    return {
        "round_at": canonical_round_at,
        "schema_version": schema_version,
        "run_id": run_id,
        "started_at": canonical_started_at,
        "finished_at": canonical_finished_at,
    }


def normalize_graphql_item(node: Mapping[str, Any], expected_type: str,
                           operation: str) -> dict[str, Any]:
    typename = node.get("__typename")
    want_typename = "Issue" if expected_type == "issue" else "PullRequest"
    if typename != want_typename:
        raise SourceFailure(
            f"{operation}: expected {want_typename}, got {typename!r}")
    repo = (node.get("repository") or {}).get("nameWithOwner") \
        if isinstance(node.get("repository"), dict) else None
    number, title, url = node.get("number"), node.get("title"), node.get("url")
    if (not _valid_slug(repo)
            or isinstance(number, bool) or not isinstance(number, int)
            or number <= 0 or not isinstance(title, str) or not title
            or not isinstance(url, str) or not url):
        raise SourceFailure(f"{operation}: malformed issue/PR node")
    _, updated_at = _timestamp(
        node.get("updatedAt"), f"{operation}: item updatedAt")
    head_owner = None
    head_ref = None
    base_ref = None
    draft = None
    if expected_type == "pull_request":
        head_owner = _login(node.get("headRepositoryOwner"))
        head_ref = node.get("headRefName")
        base_ref = node.get("baseRefName")
        draft = node.get("isDraft")
        if (not isinstance(head_owner, str) or not head_owner
                or not isinstance(head_ref, str) or not head_ref
                or not isinstance(base_ref, str) or not base_ref
                or not isinstance(draft, bool)):
            raise SourceFailure(
                f"{operation}: pull request lacks actionable head/base/draft "
                "details")
    return {
        "repository": repo,
        "type": expected_type,
        "number": number,
        "title": title,
        "url": url,
        "author": _login(node.get("author")),
        "head_owner": head_owner,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "draft": draft,
        "updated_at": updated_at,
    }


def normalize_delivery_pr(node: Mapping[str, Any], operation: str
                          ) -> dict[str, Any]:
    """Normalize a vibe-ic PR without letting unrelated malformed PRs veto us.

    Stable repository/number/url/title/time identity remains mandatory. Head
    details are retained even when absent or wrong so the producer predicate
    can exclude the row with an exact reason. A PR that *does* match the
    producer-owned head remains unfinished even if its title/base/draft drifted;
    those discrepancies are anomalies, not a way to escape the gate.
    """
    if node.get("__typename") != "PullRequest":
        raise SourceFailure(f"{operation}: delivery node is not a PullRequest")
    repo = ((node.get("repository") or {}).get("nameWithOwner")
            if isinstance(node.get("repository"), dict) else None)
    number, title, url = node.get("number"), node.get("title"), node.get("url")
    if (not isinstance(repo, str) or repo.lower() != DELIVERY_REPOSITORY
            or isinstance(number, bool) or not isinstance(number, int)
            or number <= 0 or not isinstance(title, str) or not title
            or not isinstance(url, str) or not url):
        raise SourceFailure(f"{operation}: malformed delivery PR identity")
    _, updated_at = _timestamp(
        node.get("updatedAt"), f"{operation}: PR #{number} updatedAt")
    return {
        "repository": DELIVERY_REPOSITORY,
        "type": "pull_request",
        "number": number,
        "title": title,
        "url": url,
        "author": _login(node.get("author")),
        "head_owner": _login(node.get("headRepositoryOwner")),
        "head_ref": node.get("headRefName"),
        "base_ref": node.get("baseRefName"),
        "draft": node.get("isDraft"),
        "updated_at": updated_at,
    }


def delivery_pr_decision(item: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """Return producer kind (or None) and auditable exclusion/anomaly reasons."""
    if item.get("head_owner") != ORG:
        return None, ["head_owner_is_not_vibeic"]
    head_ref = item.get("head_ref")
    if not isinstance(head_ref, str):
        return None, ["head_ref_is_missing"]
    matched: tuple[str, re.Match[str]] | None = None
    for kind, pattern in DELIVERY_PR_HEAD_PATTERNS.items():
        match = pattern.fullmatch(head_ref)
        if match is not None:
            matched = (kind, match)
            break
    if matched is None:
        return None, ["head_ref_is_not_a_delivery_producer_branch"]

    kind, match = matched
    anomalies: list[str] = []
    if item.get("base_ref") != "main":
        anomalies.append("base_ref_is_not_main")
    if item.get("draft") is not False:
        anomalies.append("draft_is_not_false")
    title = item.get("title")
    token = match.group(1)
    if kind == "anchor":
        expected = f"chore(image-anchor): adopt vibeic-eda {token}"
        if title != expected:
            anomalies.append("title_does_not_match_anchor_producer")
    elif kind == "sync":
        if not isinstance(title, str) or not title.startswith(
                f"[eda-fork] {token}: "):
            anomalies.append("title_does_not_match_sync_producer")
    elif (not isinstance(title, str)
          or not title.startswith(
              f"[eda-fork] {token}: upstream release assessment — ")):
        anomalies.append("title_does_not_match_assessment_producer")
    return kind, anomalies


def _round_rows(raw: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(raw, dict):
        raise SourceFailure("round_json: root is not an object")
    rows: dict[str, Mapping[str, Any]] = {}
    if "forks" in raw:
        if not isinstance(raw["forks"], list):
            raise SourceFailure("round_json: forks is not a list")
        candidates = []
        for pos, row in enumerate(raw["forks"]):
            if not isinstance(row, dict):
                raise SourceFailure(f"round_json: forks[{pos}] is not an object")
            name = row.get("fork") or row.get("tool") or row.get("repo")
            if isinstance(name, str) and "/" in name:
                name = name.split("/", 1)[1]
            candidates.append((name, row))
    else:
        candidates = [(name, row) for name, row in raw.items()
                      if isinstance(name, str) and not name.startswith("_")]
    for name, row in candidates:
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise SourceFailure("round_json: fork row lacks a usable name")
        key = name.lower()
        if key in rows:
            raise SourceFailure(f"round_json: duplicate fork row {name}")
        rows[key] = row
    return rows


def _nested_head_owner(item: Mapping[str, Any]) -> str | None:
    for key in ("head_owner", "headOwner"):
        if isinstance(item.get(key), str):
            return item[key]
    owner = _login(item.get("headRepositoryOwner"))
    if owner:
        return owner
    head = item.get("head")
    if isinstance(head, dict):
        repo = head.get("repo") or head.get("repository")
        if isinstance(repo, dict):
            owner = _login(repo.get("owner"))
            if owner:
                return owner
        label = head.get("label")
        if isinstance(label, str) and ":" in label:
            return label.split(":", 1)[0]
    return None


def normalize_round_pr(item: Any, expected_repo: str,
                       source: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise SourceFailure(f"{source}: upstream PR item is not an object")
    repo = item.get("repository") or item.get("repo") or expected_repo
    if isinstance(repo, dict):
        repo = repo.get("nameWithOwner") or repo.get("full_name")
    if not isinstance(repo, str) or repo.lower() != expected_repo.lower():
        raise SourceFailure(
            f"{source}: PR repository {repo!r} is not {expected_repo}")
    item_type = item.get("type", "pull_request")
    if item_type not in ("pull_request", "pr", "PullRequest"):
        raise SourceFailure(f"{source}: upstream item is not a pull request")
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise SourceFailure(f"{source}: upstream PR has invalid number")
    head_owner = _nested_head_owner(item)
    if head_owner is not None and head_owner.lower() != ORG:
        raise SourceFailure(
            f"{source}: PR #{number} head owner is {head_owner!r}, not {ORG}")
    title = item.get("title")
    url = item.get("url")
    head_ref = item.get("head_ref")
    base_ref = item.get("base_ref")
    draft = item.get("draft")
    _, updated_at = _timestamp(
        item.get("updated_at"), f"{source}: PR #{number} updated_at")
    if (not isinstance(title, str) or not title
            or not isinstance(url, str) or not url
            or not isinstance(head_ref, str) or not head_ref
            or not isinstance(base_ref, str) or not base_ref
            or not isinstance(draft, bool)):
        raise SourceFailure(
            f"{source}: PR #{number} lacks actionable "
            "title/url/head_ref/base_ref/draft details")
    return {
        "repository": expected_repo,
        "type": "pull_request",
        "number": number,
        "title": title,
        "url": url,
        "author": (_login(item.get("author"))
                   or _login(item.get("user"))),
        "head_owner": ORG,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "draft": draft,
        "updated_at": updated_at,
    }


def _scopes(repo: str, registry: Registry) -> list[str]:
    key = repo.lower()
    result: list[str] = []
    if key in registry.registered_repos:
        result.append("registered_fork")
    if key in registry.upstream_repos:
        result.append("upstream")
    if key == f"{ORG}/vibeic-eda":
        result.append("vibeic_eda")
    return result


class ItemAccumulator:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, int], dict[str, Any]] = {}

    def add(self, item: Mapping[str, Any], source: str,
            scopes: Sequence[str]) -> None:
        key = (str(item["repository"]).lower(), str(item["type"]),
               int(item["number"]))
        if key not in self._items:
            self._items[key] = {**item, "sources": [source],
                                "scopes": sorted(set(scopes))}
            return
        old = self._items[key]
        conflict_fields = (
            "title", "url", "head_owner", "head_ref", "base_ref", "draft",
            "updated_at",
        )
        conflicts = [field for field in conflict_fields
                     if old.get(field) != item.get(field)]
        if conflicts:
            identity = f"{item['repository']} {item['type']} #{item['number']}"
            raise SourceFailure(
                f"{source}: conflicting duplicate {identity} field(s): "
                + ", ".join(conflicts))
        old["sources"] = sorted(set(old["sources"]) | {source})
        old["scopes"] = sorted(set(old["scopes"]) | set(scopes))
        if not old.get("author") and item.get("author"):
            old["author"] = item["author"]

    def items(self) -> list[dict[str, Any]]:
        return sorted(self._items.values(), key=lambda x: (
            x["repository"].lower(), x["type"], x["number"]))


def _stamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        raise ValueError("clock returned a naive datetime")
    return value.isoformat(timespec="seconds")


def collect(forks_json: Path, round_json: Path, *, round_at: str,
            expected_actor: str | None = None, runner: Runner | None = None,
            now: Callable[[], datetime] | None = None) -> tuple[dict[str, Any], int]:
    runner = runner or GhGraphQLRunner()
    now = now or (lambda: datetime.now().astimezone())
    started_at = _stamp(now)
    collector_started_at, _ = _timestamp(
        started_at, "collector.started_at")
    errors: list[dict[str, str]] = []
    acc = ItemAccumulator()
    viewer_actor: str | None = None
    round_provenance: dict[str, Any] = {
        "round_at": round_at,
        "schema_version": None,
        "run_id": None,
        "started_at": None,
        "finished_at": None,
    }
    source_counts = {
        "exact_repository_issues": 0,
        "exact_repository_pull_requests": 0,
        "fork_delivery_pull_requests": 0,
        "viewer_upstream_issues": 0,
        "viewer_upstream_pull_requests": 0,
        "round_upstream_pull_requests": 0,
    }

    try:
        registry = load_registry(Path(forks_json))
    except SourceFailure as exc:
        registry = None
        errors.append({"source": "forks_json", "error": str(exc)})

    try:
        round_raw = _read_json(Path(round_json), "round_json")
        round_provenance = _round_provenance(
            round_raw, round_at, collector_started_at)
        rows = _round_rows(round_raw)
    except SourceFailure as exc:
        rows = None
        errors.append({"source": "round_json.provenance", "error": str(exc)})

    actor_check_enabled = True
    if (not isinstance(expected_actor, str) or not expected_actor
            or expected_actor.strip() != expected_actor):
        actor_check_enabled = False
        errors.append({
            "source": "viewer.identity",
            "error": "expected actor must be a non-empty exact login",
        })

    def validate_viewer(response: Mapping[str, Any], operation: str) -> None:
        nonlocal viewer_actor
        root = _graphql_root(response, operation)
        data = _mapping(root.get("data"), f"{operation}.data")
        viewer = _mapping(data.get("viewer"), f"{operation}.data.viewer")
        actor = viewer.get("login")
        if not isinstance(actor, str) or not actor:
            raise SourceFailure(
                f"{operation}: viewer.login is missing or malformed")
        if viewer_actor is None:
            viewer_actor = actor
        elif actor != viewer_actor:
            raise SourceFailure(
                f"{operation}: authenticated viewer changed from "
                f"{viewer_actor!r} to {actor!r} during collection")
        if (actor_check_enabled and expected_actor is not None
                and actor != expected_actor):
            raise SourceFailure(
                f"{operation}: authenticated viewer {actor!r} is not "
                f"expected actor {expected_actor!r}")

    canonical_repositories = (sorted(registry.registered_repos.values(),
                                     key=str.lower)
                              if registry is not None else [])
    required_repositories = sorted(
        canonical_repositories + [VIBEIC_EDA_REPOSITORY], key=str.lower)
    catalog_record: dict[str, Any] = {
        "outcome": "norecord", "total_repositories": None,
        "repositories": None, "live_fork_repositories": None,
        "unregistered_live_fork_repositories": None,
    }
    inventory_rows: list[dict[str, Any]] = []
    catalog_entries: dict[str, _CatalogRepository] = {}
    inventory_problem = False
    network_frozen = registry is None

    def validate_catalog(response: Mapping[str, Any], operation: str) -> None:
        validate_viewer(response, operation)

    def validate_repository(response: Mapping[str, Any], operation: str,
                            expected_repo: str) -> None:
        validate_viewer(response, operation)
        root = _mapping(response, operation)
        if root.get("errors"):
            return
        data = _mapping(root.get("data"), f"{operation}.data")
        repo = _mapping(data.get("repository"), f"{operation}.repository")
        actual = repo.get("nameWithOwner")
        if not isinstance(actual, str) or actual.lower() != expected_repo.lower():
            raise SourceFailure(
                f"{operation}: repository identity {actual!r} is not "
                f"{expected_repo!r}")

    if registry is not None:
        try:
            total, catalog_entries = _catalog_repositories(
                runner, validate_catalog)
            catalog_list = [{
                "repository": entry.repository,
                "is_fork": entry.is_fork,
                "is_archived": entry.is_archived,
                "has_issues_enabled": entry.has_issues_enabled,
            } for entry in sorted(catalog_entries.values(),
                                  key=lambda entry: entry.repository.lower())]
            live_forks = sorted(
                (entry.repository for entry in catalog_entries.values()
                 if entry.is_fork and not entry.is_archived), key=str.lower)
            unregistered = sorted(
                (repo for repo in live_forks
                 if repo.lower() not in registry.registered_repos), key=str.lower)
            catalog_record = {
                "outcome": "complete", "total_repositories": total,
                "repositories": catalog_list,
                "live_fork_repositories": live_forks,
                "unregistered_live_fork_repositories": unregistered,
            }
            missing = [repo for repo in required_repositories
                       if repo.lower() not in catalog_entries]
            if missing:
                inventory_problem = True
                errors.append({
                    "source": "organization.repositories",
                    "error": "required repositories absent from exact catalog: "
                             + ", ".join(missing),
                })
            if unregistered:
                inventory_problem = True
                errors.append({
                    "source": "organization.repositories",
                    "error": "live forks absent from FORKS.json: "
                             + ", ".join(unregistered),
                })
        except SourceFailure as exc:
            inventory_problem = True
            network_frozen = True
            errors.append({"source": "organization.repositories",
                           "error": str(exc)})

        for repository in required_repositories:
            scope = ("registered_fork"
                     if repository.lower() in registry.registered_repos
                     else "vibeic_eda")
            empty = {
                "repository": repository, "scope": scope, "status": "norecord",
                "is_fork": None, "is_archived": None,
                "has_issues_enabled": None,
                "issues": {"outcome": "norecord", "open_count": None},
                "pull_requests": {"outcome": "norecord", "open_count": None},
            }
            entry = catalog_entries.get(repository.lower())
            if network_frozen or entry is None:
                inventory_rows.append(empty)
                continue
            try:
                issue_nodes, pr_nodes = _finish_repository_pages(
                    runner, entry, validate_repository)
                normalized_issues = [normalize_graphql_item(
                    node, "issue", f"repository.{repository}.issues")
                    for node in issue_nodes]
                normalized_prs = [normalize_graphql_item(
                    node, "pull_request",
                    f"repository.{repository}.pull_requests")
                    for node in pr_nodes]
                item_scopes = _scopes(repository, registry)
                for item in normalized_issues:
                    acc.add(item, "exact_repository_issues", item_scopes)
                for item in normalized_prs:
                    acc.add(item, "exact_repository_pull_requests", item_scopes)
                source_counts["exact_repository_issues"] += len(
                    normalized_issues)
                source_counts["exact_repository_pull_requests"] += len(
                    normalized_prs)
                inventory_rows.append({
                    "repository": entry.repository, "scope": scope,
                    "status": "complete", "is_fork": entry.is_fork,
                    "is_archived": entry.is_archived,
                    "has_issues_enabled": entry.has_issues_enabled,
                    "issues": {
                        "outcome": ("complete" if entry.has_issues_enabled
                                    else "disabled"),
                        "open_count": len(normalized_issues),
                    },
                    "pull_requests": {
                        "outcome": "complete",
                        "open_count": len(normalized_prs),
                    },
                })
            except SourceFailure as exc:
                inventory_problem = True
                network_frozen = True
                errors.append({
                    "source": f"repository.{repository}.open_items",
                    "error": str(exc),
                })
                inventory_rows.append(empty)

    completed_inventory = sum(row["status"] == "complete"
                              for row in inventory_rows)
    inventory = {
        "status": ("complete" if registry is not None
                   and not inventory_problem
                   and completed_inventory == len(required_repositories)
                   else "norecord"),
        "catalog": catalog_record,
        "canonical_registered_repositories": canonical_repositories,
        "required_repositories": required_repositories,
        "required_repository_count": len(required_repositories),
        "completed_repository_count": completed_inventory,
        "repositories": inventory_rows,
    }

    delivery = {
        "repository": DELIVERY_REPOSITORY,
        "status": "norecord",
        "pr_head_patterns": [pattern.pattern
                             for pattern in DELIVERY_PR_HEAD_PATTERNS.values()],
        "issues": {"outcome": "excluded_no_durable_producer_marker"},
        "pull_requests": {
            "outcome": "norecord", "open_count": None,
            "matched_count": None, "excluded_count": None, "excluded": None,
        },
    }
    if registry is not None and not network_frozen:
        entry = catalog_entries.get(DELIVERY_REPOSITORY)
        if entry is None:
            errors.append({"source": "fork_delivery.pull_requests",
                           "error": "vibeic/vibe-ic absent from exact catalog"})
            network_frozen = True
        else:
            try:
                _ignored_issues, pr_nodes = _finish_repository_pages(
                    runner, entry, validate_repository, include_issues=False)
                excluded: list[dict[str, Any]] = []
                matched = 0
                for node in pr_nodes:
                    item = normalize_delivery_pr(
                        node, "fork_delivery.pull_requests")
                    kind, reasons = delivery_pr_decision(item)
                    if kind is None:
                        excluded.append({**item, "reasons": reasons})
                        continue
                    delivered = {**item, "delivery_kind": kind,
                                 "delivery_anomalies": reasons}
                    acc.add(delivered, "fork_delivery_pull_requests",
                            ["fork_delivery"])
                    matched += 1
                source_counts["fork_delivery_pull_requests"] = matched
                delivery.update(status="complete", pull_requests={
                    "outcome": "complete", "open_count": len(pr_nodes),
                    "matched_count": matched,
                    "excluded_count": len(excluded), "excluded": excluded,
                })
            except SourceFailure as exc:
                network_frozen = True
                errors.append({"source": "fork_delivery.pull_requests",
                               "error": str(exc)})

    upstream_actor = {
        "status": "norecord",
        "issues": {"outcome": "norecord", "seen_count": None,
                   "matched_count": None},
        "pull_requests": {"outcome": "norecord", "seen_count": None,
                          "matched_count": None},
    }
    if registry is not None and not network_frozen:
        actor_parts: dict[str, dict[str, Any]] = {}
        viewer_specs = (
            ("viewer_upstream.issues", "issue", VIEWER_ISSUES_QUERY, "issues",
             "viewer_upstream_issues"),
            ("viewer_upstream.pull_requests", "pull_request", VIEWER_PRS_QUERY,
             "pullRequests", "viewer_upstream_pull_requests"),
        )
        for operation, kind, query, connection_name, count_name in viewer_specs:
            if network_frozen:
                break
            try:
                nodes = paginate_connection(
                    runner, operation=operation, query=query, variables={},
                    path=("viewer", connection_name), count_field="totalCount",
                    page_validator=validate_viewer)
                matched_items = []
                for node in nodes:
                    repo = ((node.get("repository") or {}).get("nameWithOwner")
                            if isinstance(node.get("repository"), dict) else None)
                    if not isinstance(repo, str) \
                            or repo.lower() not in registry.upstream_repos:
                        continue
                    matched_items.append(normalize_graphql_item(
                        node, kind, operation))
                for item in matched_items:
                    acc.add(item, "viewer_upstream",
                            _scopes(item["repository"], registry))
                source_counts[count_name] = len(matched_items)
                actor_parts[connection_name] = {
                    "outcome": "complete", "seen_count": len(nodes),
                    "matched_count": len(matched_items),
                }
            except SourceFailure as exc:
                network_frozen = True
                errors.append({"source": operation, "error": str(exc)})
        if len(actor_parts) == 2:
            upstream_actor = {
                "status": "complete", "issues": actor_parts["issues"],
                "pull_requests": actor_parts["pullRequests"],
            }

    round_rows: list[dict[str, Any]] = []
    if registry is not None:
        for tool in registry.tools:
            source = f"round.{tool}.upstream_pull_requests"
            expected_upstream = registry.upstream_by_tool[tool]
            round_row = {"tool": tool, "repository": expected_upstream,
                         "outcome": "norecord", "open_count": None}
            row = rows.get(tool.lower()) if rows is not None else None
            measurement = row.get("upstream_pull_requests") \
                if isinstance(row, dict) else None
            if not isinstance(measurement, dict):
                if rows is not None:
                    errors.append({
                        "source": source,
                        "error": "missing repo measurement; zero is unknowable",
                    })
                round_rows.append(round_row)
                continue
            outcome = measurement.get("outcome")
            measured_upstream = measurement.get("upstream_repository")
            fork_owner = measurement.get("fork_owner")
            open_count = measurement.get("open_count")
            measured_items = measurement.get("items")
            envelope_errors = []
            if (not isinstance(measured_upstream, str)
                    or measured_upstream.lower() != expected_upstream.lower()):
                envelope_errors.append(
                    f"upstream_repository={measured_upstream!r}, expected "
                    f"{expected_upstream!r}")
            if fork_owner != ORG:
                envelope_errors.append(
                    f"fork_owner={fork_owner!r}, expected {ORG!r}")
            if (isinstance(open_count, bool) or not isinstance(open_count, int)
                    or open_count < 0):
                envelope_errors.append(
                    f"open_count={open_count!r} is not a non-negative integer")
            if not isinstance(measured_items, list):
                envelope_errors.append("items is not a list")
            elif (isinstance(open_count, int) and not isinstance(open_count, bool)
                  and open_count != len(measured_items)):
                envelope_errors.append(
                    f"open_count={open_count} but items has "
                    f"{len(measured_items)} row(s)")
            if outcome != "complete":
                envelope_errors.append(f"outcome={outcome!r}; expected complete")
            if envelope_errors:
                errors.append({"source": source,
                               "error": "; ".join(envelope_errors)})
                round_rows.append(round_row)
                continue
            try:
                normalized = [normalize_round_pr(
                    raw_item, expected_upstream, source)
                    for raw_item in measured_items]
                for item in normalized:
                    acc.add(item, "round_upstream_pull_requests",
                            _scopes(item["repository"], registry))
                source_counts["round_upstream_pull_requests"] += len(normalized)
                round_rows.append({"tool": tool,
                                   "repository": expected_upstream,
                                   "outcome": "complete",
                                   "open_count": len(normalized)})
            except SourceFailure as exc:
                errors.append({"source": source, "error": str(exc)})
                round_rows.append(round_row)

    round_upstream = {
        "status": ("complete" if registry is not None and rows is not None
                   and len(round_rows) == len(canonical_repositories)
                   and all(row["outcome"] == "complete" for row in round_rows)
                   else "norecord"),
        "required_fork_count": len(canonical_repositories),
        "completed_fork_count": sum(row["outcome"] == "complete"
                                    for row in round_rows),
        "forks": round_rows,
    }

    all_items = acc.items()
    alarm_items = [i for i in all_items if _is_alarm(i)]
    owed = [i for i in all_items if not _is_alarm(i)]
    upstream_issue_items = [i for i in owed if _is_upstream_issue(i)]
    items = [i for i in owed if not _is_upstream_issue(i)]
    issue_count = sum(item["type"] == "issue" for item in items)
    pr_count = sum(item["type"] == "pull_request" for item in items)
    counts = {
        "items": len(items),
        "issues": issue_count,
        "pull_requests": pr_count,
        "registered_fork_items": sum(
            "registered_fork" in item["scopes"] for item in items),
        "upstream_items": sum("upstream" in item["scopes"] for item in items),
        "vibeic_eda_items": sum(
            "vibeic_eda" in item["scopes"] for item in items),
        "fork_delivery_items": sum(
            "fork_delivery" in item["scopes"] for item in items),
        "by_source": source_counts,
        # REPORTED, NOT HIDDEN. A silent exclusion is how a filter becomes a
        # blind spot: the next reader sees "0 open" and cannot tell whether
        # nothing was open or something was quietly dropped.
        "alarm_items_excluded": len(alarm_items),
        # Same rule, second kind: an upstream issue we filed is a report we
        # already delivered, so it is counted HERE and not in `items`.
        "upstream_issue_items_excluded": len(upstream_issue_items),
    }
    # The excluded notices themselves, so a reader can see WHICH ones.
    alarm_record = alarm_items
    if errors:
        status, rc = "norecord", RC_NORECORD
    elif items:
        status, rc = "open", RC_OPEN
    else:
        status, rc = "converged", RC_CONVERGED
    record = {
        "schema_version": SCHEMA_VERSION,
        "program": PROGRAM,
        "started_at": started_at,
        "completed_at": _stamp(now),
        "actor": viewer_actor,
        "expected_actor": expected_actor,
        "round": round_provenance,
        "inventory": inventory,
        "delivery": delivery,
        "upstream_actor": upstream_actor,
        "round_upstream": round_upstream,
        "status": status,
        "counts": counts,
        "alarm_items": alarm_record,
        "upstream_issue_items": upstream_issue_items,
        "items": items,
        "source_errors": errors,
    }
    return record, rc


def _write_atomic(path: Path, record: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive finite number of seconds") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive finite number of seconds")
    return parsed


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None,
         now: Callable[[], datetime] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--forks-json", default=str(Path(__file__).with_name("FORKS.json")))
    parser.add_argument("--round-json", required=True)
    parser.add_argument("--round-at", required=True)
    parser.add_argument(
        "--expected-actor", default=os.environ.get("GK_GITHUB_ACTOR"))
    parser.add_argument(
        "--gh-request-deadline", "--gh-request-deadline-seconds",
        dest="gh_request_deadline", type=_positive_seconds,
        default=_positive_seconds(os.environ.get(
            "GK_GITHUB_REQUEST_DEADLINE_SECONDS",
            str(DEFAULT_GH_REQUEST_DEADLINE_S))),
        help="operational deadline per gh GraphQL request; timeout is NORECORD")
    parser.add_argument("--json", required=True)
    args = parser.parse_args(argv)

    effective_runner = runner or GhGraphQLRunner(args.gh_request_deadline)
    record, rc = collect(Path(args.forks_json), Path(args.round_json),
                         round_at=args.round_at,
                         expected_actor=args.expected_actor,
                         runner=effective_runner, now=now)
    try:
        _write_atomic(Path(args.json), record)
    except OSError as exc:
        print(f"fork_github_convergence: could not write {args.json}: {exc}",
              file=sys.stderr)
        return RC_NORECORD
    # The excluded alarms are NAMED on the same line as the count they are not
    # part of. A reader who sees "converged — 0 open" must be able to tell that
    # from "converged, and one notice was set aside".
    excluded = record["counts"].get("alarm_items_excluded", 0)
    # Likewise for the upstream reports: NAMED with their repository, because
    # "1 upstream issue" on a fleet of 38 upstreams tells a reader nothing.
    upstream_excluded = record["counts"].get(
        "upstream_issue_items_excluded", 0)
    print(f"fork_github_convergence: {record['status']} — "
          f"{record['counts']['items']} open item(s), "
          f"{len(record['source_errors'])} source error(s)"
          + (f"; {excluded} publish-silence alarm(s) NOT counted as open work "
             f"({', '.join('#' + str(i['number']) for i in record['alarm_items'])})"
             if excluded else "")
          + (f"; {upstream_excluded} upstream issue(s) NOT counted as "
             f"open work ("
             f"{', '.join(i['repository'] + '#' + str(i['number']) for i in record['upstream_issue_items'])})"
             if upstream_excluded else ""))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
