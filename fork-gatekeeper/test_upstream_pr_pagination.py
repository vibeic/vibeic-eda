#!/usr/bin/env python3
"""Upstream PR protection is exact, retained, and fail-closed."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D                                      # noqa: E402


@pytest.fixture(autouse=True)
def _the_staged_forks_root_is_the_whole_registered_fleet(monkeypatch):
    """This file stages its own forks root; its registered fleet is exactly the
    clones it staged.  Without this the round would read the production
    FORKS.json and clone every fork it registers into the temporary root."""
    monkeypatch.setattr(D, "registered_fleet", lambda path=None: (
        {p.name: {"tool": p.name} for p in D.FORKS.iterdir()
         if (p / ".git").is_dir()} if D.FORKS.is_dir() else {}, []))


G = ("git", "-C", "/work/fork")
TIP_A = "a" * 40
TIP_B = "b" * 40
TIP_C = "c" * 40


def _cp(args=("git",), rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, rc, stdout, stderr)


def _complete_prs(*items):
    return {
        "outcome": "complete", "upstream_repository": "upstream/tool",
        "fork_owner": "vibeic", "open_count": len(items),
        "items": list(items),
    }


def _pr(number, owner="somebody", ref=None):
    return {
        "number": number,
        "html_url": f"https://github.com/upstream/tool/pull/{number}",
        "title": f"change {number}",
        "state": "open",
        "draft": False,
        "updated_at": "2026-08-17T03:04:05Z",
        "head": {
            "ref": ref or f"work-{number}",
            "repo": {"owner": {"login": owner}},
        },
        "base": {"ref": "main"},
    }


def _github_remotes(monkeypatch):
    def answer(*args, cwd=None):
        del cwd
        if args[-1] == "origin":
            return "git@github.com:vibeic/tool.git"
        if args[-1] == "upstream":
            return "https://github.com/upstream/tool.git"
        raise AssertionError(args)

    monkeypatch.setattr(D, "out", answer)


def _api(monkeypatch, stdout, rc=0, stderr=""):
    seen = []

    def run(*args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, rc, stdout, stderr)

    monkeypatch.setattr(D, "sh", run)
    return seen


def test_page_two_is_included_after_more_than_one_hundred_open_prs(monkeypatch):
    _github_remotes(monkeypatch)
    first = [_pr(i, owner="foreign") for i in range(1, 101)]
    second = [_pr(101, owner="VIBEIC", ref="vibeic/page-two-fix")]
    seen = _api(monkeypatch, json.dumps([first, second]))

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence == {
        "outcome": "complete",
        "upstream_repository": "upstream/tool",
        "fork_owner": "vibeic",
        "open_count": 1,
        "items": [{
            "number": 101,
            "url": "https://github.com/upstream/tool/pull/101",
            "title": "change 101",
            "head_ref": "vibeic/page-two-fix",
            "base_ref": "main",
            "draft": False,
            "updated_at": "2026-08-17T03:04:05Z",
        }],
    }
    command = seen[0][0]
    assert "--paginate" in command and "--slurp" in command
    assert "state=open&per_page=100" in command[2]


def test_no_open_pr_is_a_complete_zero_not_norecord(monkeypatch):
    _github_remotes(monkeypatch)
    _api(monkeypatch, "[[]]")

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "complete"
    assert evidence["open_count"] == 0
    assert evidence["items"] == []
    assert D._branches_serving_an_open_upstream_pr(G, evidence) == set()


def test_only_our_matching_open_pr_is_retained_with_actionable_fields(monkeypatch):
    _github_remotes(monkeypatch)
    ours = _pr(8, owner="vibeic", ref="vibeic/keep-this")
    ours["draft"] = True
    ours["base"]["ref"] = "develop"
    _api(monkeypatch, json.dumps([[_pr(7, owner="other"), ours]]))

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["open_count"] == 1
    assert evidence["items"][0] == {
        "number": 8,
        "url": "https://github.com/upstream/tool/pull/8",
        "title": "change 8",
        "head_ref": "vibeic/keep-this",
        "base_ref": "develop",
        "draft": True,
        "updated_at": "2026-08-17T03:04:05Z",
    }
    # Historical one-argument callers still receive the old set/None contract.
    assert D._branches_serving_an_open_upstream_pr(G) == {
        "vibeic/keep-this"}


@pytest.mark.parametrize("body", [
    "[[{",                                      # truncated JSON
    json.dumps([]),                             # no slurped page record
    json.dumps([_pr(1, owner="vibeic")]),       # old flat/first-page shape
    json.dumps([[_pr(1)], [_pr(2)]]),           # partial non-final page
    json.dumps([[_pr(i, owner="foreign") for i in range(1, 101)], []]),
                                                   # synthetic/truncated page 2
    json.dumps([[dict(_pr(1), updated_at=None)]]),  # partial PR schema
])
def test_malformed_partial_or_truncated_evidence_is_norecord(monkeypatch, body):
    _github_remotes(monkeypatch)
    _api(monkeypatch, body)

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "norecord"
    assert evidence["upstream_repository"] == "upstream/tool"
    assert evidence["fork_owner"] == "vibeic"
    assert evidence["open_count"] is None
    assert evidence["items"] == []
    assert evidence["reason"]
    assert D._branches_serving_an_open_upstream_pr(G, evidence) is None


def test_transport_failure_is_norecord(monkeypatch):
    _github_remotes(monkeypatch)
    _api(monkeypatch, "", rc=1, stderr="HTTP 502 from GitHub")

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "norecord"
    assert evidence["rc"] == 1
    assert "HTTP 502" in evidence["reason"]


def test_request_deadline_is_operational_norecord_not_zero(monkeypatch):
    _github_remotes(monkeypatch)
    seen = _api(monkeypatch, "", rc=D.RC_NO_ANSWER,
                stderr="no answer: request deadline")

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "norecord"
    assert evidence["open_count"] is None
    assert evidence["rc"] == D.RC_NO_ANSWER
    assert seen[0][1]["timeout"] == D.GITHUB_API_TIMEOUT_S


def test_local_remotes_are_not_github_zero_evidence(monkeypatch):
    def local(*args, cwd=None):
        del cwd
        return ("/tmp/vibeic/tool" if args[-1] == "origin"
                else "/tmp/upstream/tool")

    monkeypatch.setattr(D, "out", local)
    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "norecord"
    assert evidence["open_count"] is None
    assert "not evidence" in evidence["reason"]


def test_other_github_owner_is_norecord_before_api_or_deletion(monkeypatch):
    def remote(*args, cwd=None):
        del cwd
        return ("git@github.com:not-vibeic/tool.git" if args[-1] == "origin"
                else "https://github.com/upstream/tool.git")

    monkeypatch.setattr(D, "out", remote)
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a foreign origin must stop before the GitHub API")))

    evidence = D._upstream_pull_request_evidence(G)

    assert evidence["outcome"] == "norecord"
    assert evidence["fork_owner"] == "not-vibeic"
    assert "not the managed" in evidence["reason"]
    assert D._branches_serving_an_open_upstream_pr(G, evidence) is None


def test_norecord_prunes_nothing_and_marks_the_row_needs_human(monkeypatch):
    unknown = D._pr_norecord(
        "upstream/tool", "vibeic", "pagination response was truncated")
    monkeypatch.setattr(D, "_upstream_pull_request_evidence", lambda g: unknown)
    monkeypatch.setattr(D, "_fork_branches", lambda *args: ["origin/fix/x"])
    monkeypatch.setattr(
        D, "_is_ours_to_delete",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("branch deletion planning must stop at NORECORD")))
    rep = {}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["upstream_pull_requests"] == unknown
    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert "NORECORD" in rep["prune_skipped"]


def test_pr_opened_after_initial_measurement_protects_branch_at_delete_boundary(
        monkeypatch):
    initial = _complete_prs()
    fresh = _complete_prs({"head_ref": "fix/new-pr"})
    monkeypatch.setattr(D, "_fork_branches",
                        lambda *args: ["origin/fix/new-pr"])
    monkeypatch.setattr(D, "_is_ours_to_delete", lambda *args: True)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on", lambda *args: set())
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_cherry_probe",
                        lambda *args: ([], _cp(), ""))
    monkeypatch.setattr(D, "_ref_tip",
                        lambda *args: (TIP_A, _cp(stdout=TIP_A + "\n")))
    monkeypatch.setattr(D, "_upstream_pull_request_evidence", lambda *args: fresh)
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a newly protected branch must not be deleted")))
    rep = {"upstream_pull_requests": initial}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["prune_protected"] == ["fix/new-pr"]
    assert rep["prune_pr_rechecks"] == [{
        "branch": "origin/fix/new-pr", "outcome": "complete",
        "open_count": 1, "protected": True,
    }]


def test_norecord_at_delete_boundary_stops_before_push(monkeypatch):
    initial = _complete_prs()
    fresh = D._pr_norecord("upstream/tool", "vibeic", "GitHub did not answer")
    monkeypatch.setattr(D, "_fork_branches", lambda *args: ["origin/fix/x"])
    monkeypatch.setattr(D, "_is_ours_to_delete", lambda *args: True)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on", lambda *args: set())
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_cherry_probe",
                        lambda *args: ([], _cp(), ""))
    monkeypatch.setattr(D, "_ref_tip",
                        lambda *args: (TIP_A, _cp(stdout=TIP_A + "\n")))
    monkeypatch.setattr(D, "_upstream_pull_request_evidence", lambda *args: fresh)
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("NORECORD must stop before branch deletion")))
    rep = {"upstream_pull_requests": initial}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert "NORECORD" in rep["prune_skipped"]


def _candidate_guards(monkeypatch, branch="origin/fix/x"):
    """Install the non-interesting guards for one prune candidate."""
    monkeypatch.setattr(D, "_fork_branches", lambda *args: [branch])
    monkeypatch.setattr(D, "_is_ours_to_delete", lambda *args: True)
    monkeypatch.setattr(D, "_upstream_pull_request_evidence",
                        lambda *args: _complete_prs())
    return branch


def test_planning_git_cherry_failure_aborts_all_deletion(monkeypatch):
    _candidate_guards(monkeypatch)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: set())
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        if "cherry" in args:
            return _cp(args, 128, stderr="fatal: missing commit")
        raise AssertionError(f"planning failure must stop before {args}")

    monkeypatch.setattr(D, "sh", command)
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert rep["prune_plan_blocked"] == [{
        "branch": "origin/fix/x", "stage": "git_cherry", "rc": 128,
        "detail": "fatal: missing commit",
    }]
    assert len(commands) == 1


def test_branch_listing_command_failure_is_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: _cp(
            args, 128, stderr="fatal: packed-refs is unreadable"))

    assert D._fork_branches(G, "main") is None


def test_image_pin_ancestor_probe_error_is_unknown(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(
        D, "image_pins",
        lambda *args, **kwargs: {"fork": TIP_A})
    monkeypatch.setattr(D, "_fork_branches",
                        lambda *args: ["origin/fix/x"])
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: _cp(
            args, 128, stderr="fatal: bad object"))

    assert D._branches_an_image_pin_depends_on(G, tmp_path) is None


def test_branch_moved_after_planning_is_retained_without_delete(monkeypatch):
    _candidate_guards(monkeypatch)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: set())
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_cherry_probe",
                        lambda *args: ([], _cp(), ""))
    tips = iter((TIP_A, TIP_B))
    monkeypatch.setattr(
        D, "_ref_tip",
        lambda *args: (tip := next(tips), _cp(stdout=tip + "\n")))
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a moved branch must not be deleted")))
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert rep["prune_retained"][0]["blocked_at"] == "branch_tip"
    assert rep["prune_rechecks"][0]["expected_tip"] == TIP_A
    assert rep["prune_rechecks"][0]["observed_tip"] == TIP_B


def test_new_image_pin_at_delete_boundary_retains_branch(monkeypatch):
    branch = _candidate_guards(monkeypatch)
    pins = iter((set(), {branch}))
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: next(pins))
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_cherry_probe",
                        lambda *args: ([], _cp(), ""))
    monkeypatch.setattr(D, "_ref_tip",
                        lambda *args: (TIP_A, _cp(stdout=TIP_A + "\n")))
    monkeypatch.setattr(
        D, "sh", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a newly pinned branch must not be deleted")))
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert rep["prune_retained"][0]["blocked_at"] == "image_pin_dependency"
    assert rep["prune_pin_protected"] == [branch]


@pytest.mark.parametrize(
    ("second_rc", "second_stdout", "blocked_at"),
    [(128, "", "git_cherry"), (0, f"+ {TIP_C}\n", "unique_commits")],
)
def test_delete_boundary_requires_successful_cherry_with_no_unique_commits(
        monkeypatch, second_rc, second_stdout, blocked_at):
    _candidate_guards(monkeypatch)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: set())
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_ref_tip",
                        lambda *args: (TIP_A, _cp(stdout=TIP_A + "\n")))
    cherry_calls = 0

    def command(*args, **kwargs):
        nonlocal cherry_calls
        if "cherry" in args:
            cherry_calls += 1
            if cherry_calls == 1:
                return _cp(args)
            return _cp(args, second_rc, second_stdout,
                       "fatal: comparison failed" if second_rc else "")
        raise AssertionError(f"invalidated cherry proof must stop before {args}")

    monkeypatch.setattr(D, "sh", command)
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert cherry_calls == 2
    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert rep["prune_retained"][0]["blocked_at"] == blocked_at


def _ready_for_remote_delete(monkeypatch, local_tip=TIP_A):
    _candidate_guards(monkeypatch)
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: set())
    monkeypatch.setattr(D, "_fetch", lambda *args: (True, ""))
    monkeypatch.setattr(D, "_cherry_probe",
                        lambda *args: ([], _cp(), ""))

    def tip(_g, ref):
        value = local_tip if ref.startswith("refs/heads/") else TIP_A
        return value, _cp(stdout=value + "\n")

    monkeypatch.setattr(D, "_ref_tip", tip)


def test_remote_lease_failure_happens_before_and_preserves_local(monkeypatch):
    _ready_for_remote_delete(monkeypatch)
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        if "push" in args:
            return _cp(args, 1, stderr="stale info")
        raise AssertionError("local deletion must not follow a failed remote lease")

    monkeypatch.setattr(D, "sh", command)
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == []
    assert rep["needs_human"] is True
    assert rep["prune_retained"][0]["blocked_at"] == "remote_delete_lease"
    assert len(commands) == 1
    assert f"--force-with-lease=refs/heads/fix/x:{TIP_A}" in commands[0]
    assert ":refs/heads/fix/x" in commands[0]


def test_remote_expected_sha_delete_precedes_local_expected_sha_delete(
        monkeypatch):
    _ready_for_remote_delete(monkeypatch)
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        return _cp(args)

    monkeypatch.setattr(D, "sh", command)
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == ["origin/fix/x"]
    assert ["push" in c for c in commands] == [True, False]
    assert commands[1][-4:] == (
        "update-ref", "-d", "refs/heads/fix/x", TIP_A)
    assert rep["prune_rechecks"][0]["remote_delete"] == "complete"
    assert rep["prune_rechecks"][0]["local_delete"] == "complete"


def test_changed_local_branch_is_preserved_after_remote_delete(monkeypatch):
    _ready_for_remote_delete(monkeypatch, local_tip=TIP_B)
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        if "push" in args:
            return _cp(args)
        raise AssertionError("a changed local branch must be preserved")

    monkeypatch.setattr(D, "sh", command)
    rep = {"upstream_pull_requests": _complete_prs()}

    D.step4_prune(G, "main", rep, apply=True)

    assert rep["pruned"] == ["origin/fix/x"]
    assert rep["needs_human"] is True
    assert len(commands) == 1 and "push" in commands[0]
    assert rep["prune_local_preserved"][0]["local_tip"] == TIP_B
    assert rep["prune_rechecks"][0]["local_delete"] == "preserved_changed"


def test_every_repository_row_retains_the_structured_evidence(
        tmp_path, monkeypatch):
    forks = tmp_path / "forks"
    for name in ("has-main", "no-main"):
        (forks / name / ".git").mkdir(parents=True)
    evidence = {
        "outcome": "complete", "upstream_repository": "upstream/tool",
        "fork_owner": "vibeic", "open_count": 0, "items": [],
    }
    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "image_pins", lambda: {})
    monkeypatch.setattr(D, "_upstream_pull_request_evidence",
                        lambda g: dict(evidence))
    monkeypatch.setattr(
        D, "mainline", lambda g: "main" if g[-1].endswith("has-main") else None)
    monkeypatch.setattr(D, "out", lambda *args, **kwargs: "")
    monkeypatch.setattr(D, "step4_prune",
                        lambda g, main, rep, apply: rep.setdefault("pruned", []))
    monkeypatch.setattr(D, "step2b_ai_decisions", lambda *args, **kwargs: {})
    monkeypatch.setattr(D.round_record, "write",
                        lambda *args, **kwargs: {"stamp": "test", "rows": 2})
    report = tmp_path / "daily.json"

    assert D.main_(["--skip-build", "--json", str(report)]) == 1
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["has-main"]["upstream_pull_requests"] == evidence
    assert saved["no-main"]["upstream_pull_requests"] == evidence
    assert saved["no-main"]["needs_human"] is True
    assert saved["_round"]["schema_version"] == 1
    assert len(saved["_round"]["run_id"]) == 32
    assert saved["_round"]["finished_at"] >= saved["_round"]["started_at"]


# --------------------------------------------------------------------------
# A REST 404 on /pulls is answered by GraphQL, never by zero (vibeic-eda#176 follow-up)
# --------------------------------------------------------------------------

_REST_404 = ('{"message":"Not Found","documentation_url":"https://docs.github.com/rest/pulls/pulls#list-pull-requests","status":"404"}'
             'gh: Not Found (HTTP 404)')


def _gql_page(nodes, has_next=False, cursor=None, name="upstream/tool"):
    return {"data": {"repository": {"nameWithOwner": name, "pullRequests": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}}}}


def _gql_node(number, owner="somebody", ref=None):
    return {"number": number, "url": f"https://github.com/upstream/tool/pull/{number}",
            "title": f"change {number}", "isDraft": False, "updatedAt": "2026-09-07T03:04:05Z",
            "headRefName": ref or f"work-{number}", "baseRefName": "main", "state": "OPEN",
            "headRepositoryOwner": {"login": owner}}


def _rest_404_then_graphql(monkeypatch, gql_stdout, gql_rc=0):
    seen = []
    def run(*args, **kwargs):
        seen.append(args)
        if "graphql" in args:
            return subprocess.CompletedProcess(args, gql_rc, gql_stdout, "")
        return subprocess.CompletedProcess(args, 1, "", _REST_404)
    monkeypatch.setattr(D, "sh", run)
    return seen


def test_a_rest_404_is_answered_by_graphql_not_by_zero(monkeypatch):
    """THE REGRESSION. IHP-GmbH/ihp-sg13cmos5l: REST /pulls 404s for every
    query shape while the repository, its commits and GraphQL all answer.
    Recorded as NORECORD the fork was pruned-nothing + needs_human every
    round and the page gate never converged."""
    _github_remotes(monkeypatch)
    seen = _rest_404_then_graphql(monkeypatch, json.dumps([_gql_page([])]))
    ev = D._upstream_pull_request_evidence(G)
    assert ev["outcome"] == "complete" and ev["open_count"] == 0 and ev["items"] == []
    assert ev["via"] == "graphql" and ev["rest_status"] == 404
    assert any("graphql" in a for a in seen), "GraphQL was never asked"
    assert D._branches_serving_an_open_upstream_pr(G, ev) == set()


def test_graphql_keeps_only_our_open_prs_with_the_same_fields(monkeypatch):
    _github_remotes(monkeypatch)
    _rest_404_then_graphql(monkeypatch, json.dumps([_gql_page(
        [_gql_node(7, owner="vibeic", ref="vibeic/fix"), _gql_node(8, owner="someone-else")])]))
    ev = D._upstream_pull_request_evidence(G)
    assert ev["open_count"] == 1
    assert ev["items"] == [{"number": 7, "url": "https://github.com/upstream/tool/pull/7",
                            "title": "change 7", "head_ref": "vibeic/fix", "base_ref": "main",
                            "draft": False, "updated_at": "2026-09-07T03:04:05Z"}]
    assert D._branches_serving_an_open_upstream_pr(G, ev) == {"vibeic/fix"}


@pytest.mark.parametrize("gql_stdout,gql_rc", [
    ("", 1),                                                        # transport failure
    ("not json", 0),                                                # malformed
    (json.dumps([]), 0),                                            # no page at all
    (json.dumps([_gql_page([], has_next=True, cursor="x")]), 0),   # pagination did not finish
    (json.dumps([_gql_page([_gql_node(1)] * 3, has_next=True, cursor="x"), _gql_page([])]), 0),  # partial non-final page
    (json.dumps([{"data": {"repository": None}}]), 0),             # repository did not resolve
    (json.dumps([_gql_page([], name="other/repo")]), 0),           # answered about another repo
])
def test_an_incomplete_graphql_answer_is_still_norecord(monkeypatch, gql_stdout, gql_rc):
    """THE BIDIRECTIONAL CONTROL: the fallback must not soften the guard into
    'REST failed, so zero'. Every incomplete GraphQL shape keeps NORECORD with
    the REST reason, and prunes nothing."""
    _github_remotes(monkeypatch)
    _rest_404_then_graphql(monkeypatch, gql_stdout, gql_rc)
    ev = D._upstream_pull_request_evidence(G)
    assert ev["outcome"] == "norecord" and ev["open_count"] is None
    assert "HTTP 404" in ev["reason"]
    assert D._branches_serving_an_open_upstream_pr(G, ev) is None


def test_a_rest_failure_that_is_not_404_never_asks_graphql(monkeypatch):
    """A 5xx, a timeout, a rate limit: those are not the measured quirk, and
    asking a second API on their behalf would widen a narrow exception."""
    _github_remotes(monkeypatch)
    seen = []
    def run(*args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 1, "", "gh: Internal Server Error (HTTP 500)")
    monkeypatch.setattr(D, "sh", run)
    ev = D._upstream_pull_request_evidence(G)
    assert ev["outcome"] == "norecord"
    assert not any("graphql" in a for a in seen)
