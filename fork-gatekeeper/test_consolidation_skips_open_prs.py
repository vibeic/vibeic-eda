"""Branch consolidation never merges past a pull request that is still deciding.

2026-09-27 09:17: the 05:30 round's step 2 (`step2_ours`, "merged N
branch(es)") merged `next/claude-mig104-fill-area` and
`next/claude-mig105-signoff-not-silent` into vibeic/librelane main and pushed.
Both were heads of OPEN pull requests -- #17 under a fork-gatekeeper HOLD for a
DRC/LVS defect, #16 still in verification -- and GitHub closed both as MERGED
at 01:23:43Z. The selection rule was "any origin branch holding a patch of ours
absent from both mainlines"; it never asked whether a PR was deciding it.

The merge path is driven with REAL git over real repositories, so "skipped"
means the mainline really did not move and "merged" means it really did. The
one thing that cannot be staged locally -- GitHub's answer -- is injected at
`_fork_branch_pull_requests`, whose own parsing is tested separately below
against canned `gh api graphql` output, including the failure shapes.
"""
import importlib
import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D                                     # noqa: E402
import check_our_commits_ship as S                         # noqa: E402

BRANCH = "next/claude-mig104-fill-area"


def _run(*a, cwd=None):
    r = subprocess.run(a, capture_output=True, text=True, cwd=cwd)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


def _commit(repo, fname, text, msg, who="reyerchu"):
    (repo / fname).write_text(text, encoding="utf-8")
    _run("git", "add", fname, cwd=str(repo))
    _run("git", "-c", f"user.email={who}@example.invalid",
         "-c", f"user.name={who}", "commit", "-q", "-m", msg, cwd=str(repo))
    return _run("git", "rev-parse", "HEAD", cwd=str(repo))


@pytest.fixture
def fork(tmp_path):
    """A fork whose origin holds one branch with a commit of ours main lacks."""
    origin = tmp_path / "origin.git"
    _run("git", "init", "--bare", "-q", "--initial-branch=master", str(origin))
    pub = tmp_path / "pub"
    _run("git", "clone", "-q", str(origin), str(pub))
    _commit(pub, "base.txt", "base\n", "base")
    _run("git", "push", "-q", "-u", "origin", "master", cwd=str(pub))
    _run("git", "checkout", "-q", "-b", BRANCH, cwd=str(pub))
    tip = _commit(pub, "fill.txt", "fill area\n", "FillInsertion: bound the fill")
    _run("git", "push", "-q", "-u", "origin", BRANCH, cwd=str(pub))
    clone = tmp_path / "clone"
    _run("git", "clone", "-q", str(origin), str(clone))
    _run("git", "config", "user.email", "gk@example.invalid", cwd=str(clone))
    _run("git", "config", "user.name", "gatekeeper", cwd=str(clone))
    g = ("git", "-C", str(clone))
    return {"g": g, "clone": clone, "tip": tip,
            "before": _run("git", "rev-parse", "HEAD", cwd=str(clone))}


def _answer(monkeypatch, evidence):
    asked = []

    def fake(g, branch):
        asked.append(branch)
        return evidence
    monkeypatch.setattr(D, "_fork_branch_pull_requests", fake)
    # `check_our_commits_ship` imports lazily by name; another test in the
    # suite may have re-imported daily_0530, so patch whichever module that
    # import will actually resolve too.
    live = importlib.import_module("daily_0530")
    if live is not D:
        monkeypatch.setattr(live, "_fork_branch_pull_requests", fake)
    return asked


def _pr(number, state):
    return {"number": number, "state": state, "url": f"https://x/{number}",
            "created_at": "2026-09-27T00:39:51Z", "merged_at": None,
            "closed_at": None, "head_oid": "0" * 40}


def _complete(*prs):
    return {"outcome": "complete", "repository": "vibeic/librelane",
            "head_ref": BRANCH, "prs": list(prs), "via": "graphql"}


def _head(fork):
    return _run("git", "rev-parse", "HEAD", cwd=str(fork["clone"]))


def _contains_tip(fork):
    r = subprocess.run(["git", "-C", str(fork["clone"]), "merge-base",
                        "--is-ancestor", fork["tip"], "HEAD"])
    return r.returncode == 0


# ── the incident: an OPEN PR (verification in progress, #16) ────────────────
def test_open_pr_branch_is_skipped_and_main_does_not_move(fork, monkeypatch):
    asked = _answer(monkeypatch, _complete(_pr(16, "OPEN")))
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert asked == [f"origin/{BRANCH}"]
    assert rep["merged"] == []
    assert _head(fork) == fork["before"] and not _contains_tip(fork)
    held = rep["ours_held"]
    assert [(h["branch"], h["verdict"]) for h in held] == [
        (f"origin/{BRANCH}", "open_pr")]
    assert "#16" in held[0]["reason"]
    # Waiting on a PR is the normal state of review, not a failure.
    assert not rep.get("needs_human")


# ── the incident: an OPEN PR under a fork-gatekeeper HOLD review (#17) ──────
def test_hold_pr_branch_is_skipped(fork, monkeypatch):
    """HOLD is a review on an OPEN PR; the open state alone holds the merge,
    so no review text has to be parsed for the HOLD to be honoured -- and an
    earlier MERGED PR on the same head does not release it."""
    _answer(monkeypatch, _complete(_pr(9, "MERGED"), _pr(17, "OPEN")))
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert rep["merged"] == [] and not _contains_tip(fork)
    assert rep["ours_held"][0]["verdict"] == "open_pr"
    assert "#17" in rep["ours_held"][0]["reason"]


# ── a PR closed WITHOUT merging: somebody said no ───────────────────────────
def test_declined_pr_branch_is_not_merged(fork, monkeypatch):
    _answer(monkeypatch, _complete(_pr(18, "CLOSED")))
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert rep["merged"] == [] and not _contains_tip(fork)
    assert rep["ours_held"][0]["verdict"] == "declined_pr"
    assert not rep.get("needs_human")


# ── the intent that stays: a finished PR, and lane work with no PR ──────────
@pytest.mark.parametrize("prs", [(), (_pr(5, "MERGED"),),
                                 (_pr(4, "CLOSED"), _pr(5, "MERGED"))],
                         ids=["no-pr", "merged-pr", "closed-then-merged"])
def test_branch_without_open_pr_is_still_consolidated(fork, monkeypatch, prs):
    """The 2026-07-31 ruling: once the PR is done ("結束了以後") -- or when no
    PR ever existed -- our commits come onto the one line. Break this side and
    the round stops consolidating anything."""
    _answer(monkeypatch, _complete(*prs))
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert rep["merged"] == [{"branch": f"origin/{BRANCH}", "commits": 1}]
    assert _contains_tip(fork)
    assert "ours_held" not in rep and not rep.get("needs_human")


# ── PR state unknown: refusal, not merge ────────────────────────────────────
def test_pr_lookup_failure_refuses_the_merge_loudly(fork, monkeypatch, capsys):
    _answer(monkeypatch, {"outcome": "norecord", "reason": "HTTP 502",
                          "prs": None})
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert rep["merged"] == [] and not _contains_tip(fork)
    assert rep["needs_human"] is True
    assert rep["ours_held"][0]["verdict"] == "unknown"
    ev = [e for e in rep["ours_branch_evidence"]
          if e["outcome"] == "refused_pr_state_unknown"]
    assert ev and "HTTP 502" in ev[0]["reason"]
    assert "REFUSED" in capsys.readouterr().err
    # ...and the release authorizer, which refuses `merge_unmeasurable`, sees it.
    assert [m["stage"] for m in rep["merge_unmeasurable"]] == [
        "pull_request_state"]


def test_real_lookup_on_a_non_github_origin_refuses(fork):
    """No injection: a local-path origin cannot say whether GitHub holds a PR,
    so the real lookup must come back NORECORD and step 2 must refuse."""
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert rep["merged"] == [] and not _contains_tip(fork)
    assert rep["needs_human"] is True


def test_a_quiet_fork_asks_github_nothing(fork, monkeypatch):
    """The lookup runs only for a branch that would otherwise merge."""
    _run("git", "merge", "-q", "--no-edit", f"origin/{BRANCH}",
         cwd=str(fork["clone"]))
    _run("git", "push", "-q", "origin", "master", cwd=str(fork["clone"]))
    asked = _answer(monkeypatch, {"outcome": "norecord", "reason": "x"})
    rep = {}
    D.step2_ours(fork["g"], "master", rep)
    assert asked == [] and not rep.get("needs_human")


# ── `_fork_branch_pull_requests` parses GitHub's answer fail-closed ─────────
def _gh(monkeypatch, *, rc=0, body=None, stdout=None,
        origin="https://github.com/vibeic/librelane.git"):
    calls = []
    real_sh = D.sh

    def fake_sh(*a, **kw):
        if a[:2] == ("gh", "api"):
            calls.append(a)
            text = stdout if stdout is not None else json.dumps(body)
            return subprocess.CompletedProcess(a, rc, text,
                                               "" if rc == 0 else "gh: boom")
        return real_sh(*a, **kw)
    monkeypatch.setattr(D, "sh", fake_sh)
    monkeypatch.setattr(D, "out", lambda *a, **kw: origin
                        if a[-3:] == ("remote", "get-url", "origin")
                        else "")
    return calls


def _node(number, state, head=BRANCH, repo="vibeic/librelane"):
    return {"number": number, "url": f"https://x/{number}", "state": state,
            "createdAt": "2026-09-27T00:00:00Z", "mergedAt": None,
            "closedAt": None, "headRefName": head, "headRefOid": "a" * 40,
            "headRepository": {"nameWithOwner": repo}}


def _body(*nodes, total=None, name="vibeic/librelane"):
    return {"data": {"repository": {"nameWithOwner": name, "pullRequests": {
        "totalCount": len(nodes) if total is None else total,
        "nodes": list(nodes)}}}}


def test_lookup_uses_graphql_and_reads_open_state(monkeypatch):
    calls = _gh(monkeypatch, body=_body(_node(17, "OPEN")))
    ev = D._fork_branch_pull_requests(("git", "-C", "/x"), f"origin/{BRANCH}")
    assert ev["outcome"] == "complete"
    assert [(p["number"], p["state"]) for p in ev["prs"]] == [(17, "OPEN")]
    assert calls and calls[0][:3] == ("gh", "api", "graphql")
    assert f"head={BRANCH}" in calls[0]            # origin/ prefix stripped
    assert D.consolidation_verdict(ev)[0] == "open_pr"


def test_lookup_ignores_a_same_named_branch_of_another_repository(monkeypatch):
    _gh(monkeypatch, body=_body(_node(3, "OPEN", repo="someone/librelane")))
    ev = D._fork_branch_pull_requests(("git",), BRANCH)
    assert ev["outcome"] == "complete" and ev["prs"] == []


@pytest.mark.parametrize("kw", [
    {"rc": 1, "body": {}},                                   # gh failed
    {"stdout": "not json"},                                   # garbage
    {"body": {"errors": [{"message": "rate limited"}]}},      # GraphQL errors
    {"body": {"data": {"repository": None}}},                 # unresolved
    {"body": _body(_node(1, "OPEN"), total=101)},             # a page unread
    {"body": _body(_node(1, "OPEN"), name="vibeic/other")},   # wrong repo
    {"body": _body(_node(1, "WEIRD"))},                       # unknown state
    {"body": _body(), "origin": "/srv/local/librelane.git"},  # not GitHub
], ids=["rc", "json", "errors", "no-repo", "truncated", "wrong-repo",
        "state", "local-origin"])
def test_lookup_failure_is_norecord_and_verdict_unknown(monkeypatch, kw):
    _gh(monkeypatch, **kw)
    ev = D._fork_branch_pull_requests(("git",), BRANCH)
    assert ev["outcome"] == "norecord" and ev["prs"] is None
    assert D.consolidation_verdict(ev)[0] == "unknown"


def test_verdict_on_malformed_evidence_is_unknown():
    for ev in (None, {}, {"outcome": "complete"},
               {"outcome": "complete", "prs": "nope"}):
        assert D.consolidation_verdict(ev)[0] == "unknown"


# ── the auditor agrees with the merger about what is owed ────────────────────
def test_ship_check_counts_open_pr_work_as_held_not_stranded(fork, monkeypatch):
    """`check_our_commits_ship` exits 1 (tick rc=6) on stranded work. A branch
    held for its PR is not stranded, or every PR under review turns the tick
    red and invites a hand merge -- the same defect by another road."""
    pin = fork["before"]
    _answer(monkeypatch, _complete(_pr(17, "OPEN")))
    held = []
    assert S.stranded(fork["clone"], pin, held) == []
    assert [h["held"] for h in held] == ["open_pr"]


def test_ship_check_still_reports_work_when_pr_state_unknown(fork, monkeypatch):
    pin = fork["before"]
    _answer(monkeypatch, {"outcome": "norecord", "reason": "x", "prs": None})
    held = []
    rows = S.stranded(fork["clone"], pin, held)
    assert [r["branch"] for r in rows] == [f"origin/{BRANCH}"] and held == []


def test_ship_check_still_reports_work_with_no_pr(fork, monkeypatch):
    pin = fork["before"]
    _answer(monkeypatch, _complete())
    assert [r["branch"] for r in S.stranded(fork["clone"], pin)] == [
        f"origin/{BRANCH}"]
