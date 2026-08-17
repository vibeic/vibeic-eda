"""Step 2 does not manufacture AI work from a stale local mainline.

The iverilog 05:30 round started with a local master older than origin/master.
The source branch was already an ancestor of the freshly fetched remote main,
but comparing it only with local master created a conflict and an AI case.  The
tests below build that graph with real repositories and exercise both ancestry
and patch-equivalent delivery, plus the genuinely absent direction.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D  # noqa: E402


BRANCH = "vibeic/nonblocking-event"


def _git(repo: Path, *args: str, check: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env=env,
    )
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _graph(tmp_path: Path, delivered: str) -> tuple[Path, Path, str]:
    """Return stale clone, bare origin and source tip.

    ``delivered`` is ``ancestry``, ``patch``, or ``absent`` on origin/master.
    Local master independently changes the same line, so blindly merging the
    source branch into that stale line conflicts in every case.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "--initial-branch=master",
         str(origin))
    publisher = tmp_path / "publisher"
    _git(tmp_path, "clone", "-q", str(origin), str(publisher))
    _git(publisher, "config", "user.email", "vibeic@example.invalid")
    _git(publisher, "config", "user.name", "vibeic bot")
    (publisher / "shared.txt").write_text("base\n", encoding="utf-8")
    _commit(publisher, "base")
    _git(publisher, "push", "-q", "-u", "origin", "master")

    # Clone before origin/master receives the branch. Its checked-out master is
    # the stale side of the production incident.
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "local@example.invalid")
    _git(clone, "config", "user.name", "local checkout")

    _git(publisher, "checkout", "-q", "-b", BRANCH)
    (publisher / "shared.txt").write_text("branch work\n", encoding="utf-8")
    source_tip = _commit(publisher, "nonblocking event fix")
    _git(publisher, "push", "-q", "-u", "origin", BRANCH)
    _git(publisher, "checkout", "-q", "master")

    if delivered == "ancestry":
        _git(publisher, "merge", "-q", "--no-ff", "-m",
             "merge delivered fix", BRANCH)
    elif delivered == "patch":
        # A different parent guarantees a different SHA while git-cherry still
        # proves the source patch is present.
        (publisher / "unrelated.txt").write_text("remote\n", encoding="utf-8")
        _commit(publisher, "unrelated remote work")
        _git(publisher, "cherry-pick", source_tip)
        assert _git(publisher, "rev-parse", "HEAD").stdout.strip() != source_tip
    elif delivered != "absent":
        raise AssertionError(f"unknown graph kind {delivered}")
    _git(publisher, "push", "-q", "origin", "master")

    (clone / "shared.txt").write_text("local upstream spelling\n",
                                       encoding="utf-8")
    _commit(clone, "local main moved independently")
    _git(clone, "fetch", "-q", "origin")

    # This is the redundant-AI precondition: local comparison attempts a merge
    # and conflicts, although the authoritative remote may already carry it.
    conflict = _git(clone, "merge", "--no-edit", f"origin/{BRANCH}",
                    check=False)
    assert conflict.returncode != 0
    assert _git(clone, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    _git(clone, "merge", "--abort")
    return clone, origin, source_tip


def _open_pr_evidence() -> dict:
    return {
        "outcome": "complete",
        "upstream_repository": "steveicarus/iverilog",
        "fork_owner": "vibeic",
        "open_count": 1,
        "items": [{"head_ref": BRANCH}],
    }


def test_remote_ancestry_skips_stale_local_conflict_and_keeps_open_pr_branch(
        tmp_path, monkeypatch):
    clone, origin, source_tip = _graph(tmp_path, "ancestry")
    g = ["git", "-C", str(clone)]
    before = _git(clone, "rev-parse", "master").stdout.strip()
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "origin/master", check=False).returncode == 0
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "master", check=False).returncode == 1
    rep = {"upstream_pull_requests": _open_pr_evidence()}

    D.step2_ours(g, "master", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert "merge_unmeasurable" not in rep
    assert rep["carried_nothing_new"] == 1
    assert rep["ours_branch_evidence"][0]["covered_by"] == "origin/master"
    assert rep["ours_branch_evidence"][0]["checks"][-1]["method"] == "ancestry"
    assert _git(clone, "rev-parse", "master").stdout.strip() == before

    # Skipping redundant consolidation must not weaken the independent promise
    # that a branch serving an open upstream PR is retained.
    monkeypatch.setattr(D, "_branches_an_image_pin_depends_on",
                        lambda *args: set())
    D.step4_prune(g, "master", rep, apply=True)
    assert rep["pruned"] == []
    assert rep["prune_protected"] == [BRANCH]
    remote = _git(tmp_path, "ls-remote", str(origin),
                  f"refs/heads/{BRANCH}").stdout.split()
    assert remote and remote[0] == source_tip


def test_remote_patch_equivalence_skips_stale_local_conflict(tmp_path):
    clone, _origin, source_tip = _graph(tmp_path, "patch")
    g = ["git", "-C", str(clone)]
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "origin/master", check=False).returncode == 1
    cherry = _git(clone, "cherry", "origin/master", f"origin/{BRANCH}")
    assert cherry.stdout.strip() == f"- {source_tip}"
    rep = {}

    D.step2_ours(g, "master", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert "merge_unmeasurable" not in rep
    assert rep["carried_nothing_new"] == 1
    assert rep["ours_branch_evidence"][0]["covered_by"] == "origin/master"
    assert (rep["ours_branch_evidence"][0]["checks"][-1]["method"]
            == "patch_equivalence")


def test_local_main_ancestry_is_sufficient_when_remote_main_is_stale(tmp_path):
    clone, _origin, source_tip = _graph(tmp_path, "absent")
    # Model work already landed locally and awaiting this round's push. The
    # authoritative remote is deliberately still behind; "either mainline"
    # means this is complete without consulting it for branch delivery.
    _git(clone, "reset", "--hard", source_tip)
    g = ["git", "-C", str(clone)]
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "master", check=False).returncode == 0
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "origin/master", check=False).returncode == 1
    rep = {}

    D.step2_ours(g, "master", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert rep["carried_nothing_new"] == 1
    assert rep["ours_branch_evidence"][0]["covered_by"] == "master"
    assert len(rep["ours_branch_evidence"][0]["checks"]) == 1


def test_work_absent_from_both_mainlines_still_forms_real_ai_case(tmp_path):
    clone, _origin, source_tip = _graph(tmp_path, "absent")
    g = ["git", "-C", str(clone)]
    for target in ("master", "origin/master"):
        cherry = _git(clone, "cherry", target, f"origin/{BRANCH}")
        assert cherry.stdout.strip() == f"+ {source_tip}"
    rep = {}

    D.step2_ours(g, "master", rep)

    assert rep["merged"] == []
    assert len(rep["conflicted"]) == 1
    assert rep["conflicted"][0]["branch"] == f"origin/{BRANCH}"
    assert rep["conflicted"][0]["direction"] == "our branch -> our mainline"
    assert rep["conflicted"][0]["commits"] == 1
    assert rep["ours_branch_evidence"][0]["outcome"] == "outstanding"
    assert "merge_unmeasurable" not in rep


@pytest.mark.parametrize(
    ("cherry_rc", "cherry_stdout", "detail"),
    [
        (128, "", "fatal: comparison failed"),
        (0, "this is not git-cherry evidence\n", "malformed output"),
    ],
)
def test_step2_git_cherry_failure_or_malformed_output_is_norecord(
        monkeypatch, cherry_rc, cherry_stdout, detail):
    monkeypatch.setattr(D, "_fork_branches",
                        lambda *args: [f"origin/{BRANCH}"])
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        if "cherry" in args:
            return subprocess.CompletedProcess(
                args, cherry_rc, cherry_stdout,
                "fatal: comparison failed" if cherry_rc else "")
        raise AssertionError(f"invalid cherry evidence reached {args}")

    monkeypatch.setattr(D, "sh", command)
    rep = {}

    D.step2_ours(["git", "-C", "/unused"], "master", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert rep["needs_human"] is True
    assert rep["merge_unmeasurable"][0]["stage"] == "git_cherry"
    assert detail in rep["merge_unmeasurable"][0]["detail"]
    assert sum("cherry" in args for args in commands) == 2


def test_empty_cherry_walk_with_nonancestral_merge_commit_is_norecord(
        monkeypatch):
    merge_sha = "a" * 40

    def command(*args, **kwargs):
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        if "cherry" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "rev-list" in args:
            return subprocess.CompletedProcess(args, 0, merge_sha + "\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(D, "sh", command)

    unique, evidence = D._branch_work_against(
        ["git", "-C", "/unused"], "master", f"origin/{BRANCH}")

    assert unique is None
    assert evidence["outcome"] == "norecord"
    assert evidence["stage"] == "merge_commit_patch_equivalence"
