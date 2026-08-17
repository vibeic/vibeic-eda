#!/usr/bin/env python3
"""Exact GitHub issue/PR convergence record for the fork fleet.

Zero is a strong statement here: it is emitted only after all paginated GitHub
connections and every fork's round-local upstream-PR measurement are complete.
Any missing page, malformed cursor, search-cap boundary, or absent fork row is
``norecord`` rather than an invented zero.

Each fork row's ``upstream_pull_requests`` envelope is load-bearing too.  It
must contain ``outcome``, ``upstream_repository``, ``fork_owner``,
``open_count``, and ``items``; the declared count must equal the list length.
This prevents a producer that queried the wrong repository (or never formed a
list) from laundering ``complete + []`` into convergence.
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


SCHEMA_VERSION = 1
PROGRAM = "fork_github_convergence"
ORG = "vibeic"
RC_CONVERGED = 0
RC_OPEN = 1
RC_NORECORD = 2
DEFAULT_GH_REQUEST_DEADLINE_S = 120.0
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


ORG_SEARCH_QUERY = r"""
query ForkConvergenceOrgSearch($queryString: String!, $cursor: String) {
  search(query: $queryString, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    nodes {
      __typename
      ... on Issue {
        number title url updatedAt
        author { login }
        repository { nameWithOwner }
      }
      ... on PullRequest {
        number title url headRefName baseRefName isDraft updatedAt
        author { login }
        headRepositoryOwner { login }
        repository { nameWithOwner }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

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


def _connection(response: Mapping[str, Any], path: Sequence[str],
                operation: str) -> Mapping[str, Any]:
    root = _mapping(response, operation)
    if root.get("errors"):
        raise SourceFailure(
            f"{operation}: GraphQL errors: "
            + " ".join(str(root["errors"]).split())[:500])
    cur: Any = root.get("data")
    for name in path:
        cur = _mapping(cur, f"{operation}.data path").get(name)
    return _mapping(cur, f"{operation}.connection")


def paginate_connection(runner: Runner, *, operation: str, query: str,
                        variables: Mapping[str, Any], path: Sequence[str],
                        count_field: str, search_cap: bool = False,
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
            if search_cap and count >= 1000:
                raise SourceFailure(
                    f"{operation}: search count {count} reaches GitHub's "
                    "1000-result cap; completeness is unknowable")
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


def _scopes(repo: str, registry: Registry, *, org_source: bool = False) -> list[str]:
    key = repo.lower()
    result: list[str] = []
    if key in registry.registered_repos:
        result.append("registered_fork")
    if key in registry.upstream_repos:
        result.append("upstream")
    if key == f"{ORG}/vibeic-eda":
        result.append("vibeic_eda")
    if (org_source and key.startswith(f"{ORG}/")
            and key not in registry.registered_repos
            and key != f"{ORG}/vibeic-eda"):
        result.append("out_of_registry_org")
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
        "org_search_issues": 0,
        "org_search_pull_requests": 0,
        "viewer_issues_matched": 0,
        "viewer_pull_requests_matched": 0,
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
        root = _mapping(response, operation)
        if root.get("errors"):
            return  # _connection preserves the actual GraphQL error below.
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

    if registry is not None:
        org_specs = (
            ("org_issues", "issue", "is:issue", "org_search_issues"),
            ("org_pull_requests", "pull_request", "is:pr",
             "org_search_pull_requests"),
        )
        for operation, kind, qualifier, count_name in org_specs:
            try:
                nodes = paginate_connection(
                    runner, operation=operation, query=ORG_SEARCH_QUERY,
                    variables={"queryString": f"org:{ORG} is:open {qualifier}"},
                    path=("search",), count_field="issueCount", search_cap=True)
                for node in nodes:
                    item = normalize_graphql_item(node, kind, operation)
                    if not item["repository"].lower().startswith(f"{ORG}/"):
                        raise SourceFailure(
                            f"{operation}: non-{ORG} repository in org search")
                    acc.add(item, "org_search",
                            _scopes(item["repository"], registry, org_source=True))
                source_counts[count_name] = len(nodes)
            except SourceFailure as exc:
                errors.append({"source": operation, "error": str(exc)})

        viewer_specs = (
            ("viewer_issues", "issue", VIEWER_ISSUES_QUERY, "issues",
             "viewer_issues_matched"),
            ("viewer_pull_requests", "pull_request", VIEWER_PRS_QUERY,
             "pullRequests", "viewer_pull_requests_matched"),
        )
        target_repos = set(registry.registered_repos) | set(registry.upstream_repos)
        for operation, kind, query, connection_name, count_name in viewer_specs:
            try:
                nodes = paginate_connection(
                    runner, operation=operation, query=query, variables={},
                    path=("viewer", connection_name), count_field="totalCount",
                    page_validator=validate_viewer)
                matched = 0
                for node in nodes:
                    item = normalize_graphql_item(node, kind, operation)
                    if item["repository"].lower() not in target_repos:
                        continue
                    matched += 1
                    acc.add(item, "viewer",
                            _scopes(item["repository"], registry))
                source_counts[count_name] = matched
            except SourceFailure as exc:
                errors.append({"source": operation, "error": str(exc)})

        if rows is not None:
            for tool in registry.tools:
                source = f"round.{tool}.upstream_pull_requests"
                row = rows.get(tool.lower())
                measurement = row.get("upstream_pull_requests") \
                    if isinstance(row, dict) else None
                if not isinstance(measurement, dict):
                    errors.append({
                        "source": source,
                        "error": "missing repo measurement; zero is unknowable",
                    })
                    continue
                outcome = measurement.get("outcome")
                measured_upstream = measurement.get("upstream_repository")
                fork_owner = measurement.get("fork_owner")
                open_count = measurement.get("open_count")
                measured_items = measurement.get("items")
                expected_upstream = registry.upstream_by_tool[tool]
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
                elif (isinstance(open_count, int)
                      and not isinstance(open_count, bool)
                      and open_count != len(measured_items)):
                    envelope_errors.append(
                        f"open_count={open_count} but items has "
                        f"{len(measured_items)} row(s)")
                if envelope_errors:
                    errors.append({"source": source,
                                   "error": "; ".join(envelope_errors)})
                    continue
                if outcome != "complete":
                    errors.append({
                        "source": source,
                        "error": (f"outcome={outcome!r}; expected complete"
                                  if outcome in ("norecord", None)
                                  else f"malformed outcome {outcome!r}"),
                    })
                    continue
                try:
                    for raw_item in measured_items:
                        item = normalize_round_pr(
                            raw_item, expected_upstream, source)
                        acc.add(item, "round_upstream_pull_requests",
                                _scopes(item["repository"], registry))
                        source_counts["round_upstream_pull_requests"] += 1
                except SourceFailure as exc:
                    errors.append({"source": source, "error": str(exc)})
    else:
        errors.extend([
            {"source": "org_search",
             "error": "not attempted because forks_json is unusable"},
            {"source": "viewer",
             "error": "not attempted because forks_json is unusable"},
        ])

    items = acc.items()
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
        "out_of_registry_org_items": sum(
            "out_of_registry_org" in item["scopes"] for item in items),
        "by_source": source_counts,
    }
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
        "status": status,
        "counts": counts,
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
    print(f"fork_github_convergence: {record['status']} — "
          f"{record['counts']['items']} open item(s), "
          f"{len(record['source_errors'])} source error(s)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
