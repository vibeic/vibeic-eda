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
    elif delivered == "squash":
        # A second branch commit, so the squash's single patch matches no
        # individual commit's patch id -- the shape git cherry cannot see.
        _git(publisher, "checkout", "-q", BRANCH)
        (publisher / "second.txt").write_text("second\n", encoding="utf-8")
        source_tip = _commit(publisher, "second branch commit")
        _git(publisher, "push", "-q", "origin", BRANCH)
        _git(publisher, "checkout", "-q", "master")
        _git(publisher, "merge", "-q", "--squash", BRANCH)
        _commit(publisher, "squash of the branch (a merged PR)")
        # The mainline moved on afterwards, on the same file: re-merging the
        # branch now can only conflict, exactly as OpenROAD did on 2026-09-07.
        (publisher / "shared.txt").write_text("branch work, then more\n",
                                              encoding="utf-8")
        _commit(publisher, "later mainline work on the same file")
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


def _already_carried_merge_graph(tmp_path: Path, *, merge_only_change: bool):
    """A topic merge whose ordinary commits are patch-equivalent on target.

    The shape matches Trilinos' maintained image branch: both sides' patches
    are on the shipping line, but the topic's last maintenance merge is not an
    ancestor of that line.  ``merge_only_change`` plants content directly in
    the merge commit, which git-cherry cannot report and the gate must retain.
    """
    repo = tmp_path / ("evil-merge" if merge_only_change else "plain-merge")
    _git(tmp_path, "init", "-q", "--initial-branch=master", str(repo))
    _git(repo, "config", "user.email", "vibeic@example.invalid")
    _git(repo, "config", "user.name", "vibeic bot")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "topic")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = _commit(repo, "feature")

    _git(repo, "checkout", "-q", "-b", "side", base)
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    side = _commit(repo, "side")

    _git(repo, "checkout", "-q", "topic")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    if merge_only_change:
        (repo / "merge-only.txt").write_text(
            "not represented by either parent\n", encoding="utf-8")
    merge = _commit(repo, "merge side into topic")

    _git(repo, "checkout", "-q", "-b", "target", base)
    (repo / "target-only.txt").write_text("different parent\n",
                                            encoding="utf-8")
    _commit(repo, "target moved independently")
    _git(repo, "cherry-pick", feature)
    _git(repo, "cherry-pick", side)
    assert _git(repo, "merge-base", "--is-ancestor", merge, "target",
                check=False).returncode == 1
    assert all(line.startswith("- ") for line in
               _git(repo, "cherry", "target", "topic").stdout.splitlines())
    return repo, merge


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


@pytest.mark.parametrize("keep_cached_origin_head", [True, False])
def test_remote_default_repairs_a_mismatched_local_main_name(
        tmp_path, keep_cached_origin_head):
    clone, _origin, source_tip = _graph(tmp_path, "ancestry")
    # The checkout kept/renamed its local line to `main`, while the fork still
    # publishes only `master`.  This was the shape previously measured on an
    # ASAP7 mirror.  Absence of origin/main must not manufacture an AI case.
    _git(clone, "branch", "-m", "master", "main")
    if not keep_cached_origin_head:
        _git(clone, "remote", "set-head", "origin", "-d")
    assert _git(clone, "show-ref", "--verify", "--quiet",
                "refs/remotes/origin/main", check=False).returncode != 0
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "origin/master", check=False).returncode == 0
    rep = {}

    D.step2_ours(["git", "-C", str(clone)], "main", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert "merge_unmeasurable" not in rep
    assert rep["ours_remote_mainline"]["ref"] == "origin/master"
    assert rep["ours_remote_mainline"]["method"] == "live_remote_head"
    assert rep["ours_remote_mainline"]["remote_head_ref"] == (
        "refs/heads/master")
    assert rep["ours_branch_evidence"][0]["covered_by"] == "origin/master"


def test_remote_default_race_is_norecord_not_a_guessed_fallback(
        tmp_path, monkeypatch):
    clone, _origin, _source_tip = _graph(tmp_path, "ancestry")
    _git(clone, "branch", "-m", "master", "main")
    real_sh = D.sh

    def raced_remote_head(*args, **kwargs):
        if "ls-remote" in args:
            return subprocess.CompletedProcess(
                args, 0,
                "ref: refs/heads/master\tHEAD\n" + "f" * 40 + "\tHEAD\n",
                "",
            )
        return real_sh(*args, **kwargs)

    monkeypatch.setattr(D, "sh", raced_remote_head)

    target, evidence = D._authoritative_origin_mainline(
        ["git", "-C", str(clone)], "main")

    assert target is None
    assert evidence["outcome"] == "norecord"
    assert evidence["stage"] == "origin_mainline"
    assert evidence["remote_head_tip"] == "f" * 40
    assert evidence["candidates"][-1]["ref"] == "origin/master"
    assert evidence["candidates"][-1]["tip"] != "f" * 40


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


def test_work_absent_from_both_mainlines_still_forms_real_ai_case(
        tmp_path, monkeypatch):
    # The branch has no pull request, so consolidation may try it. A local
    # origin cannot say that on its own (the lookup would refuse), so GitHub's
    # answer is supplied; test_consolidation_skips_open_prs owns the PR side.
    monkeypatch.setattr(D, "_fork_branch_pull_requests", lambda g, b: {
        "outcome": "complete", "prs": [], "head_ref": b})
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
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
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


def test_empty_cherry_walk_with_unproven_merge_commit_is_norecord(
        monkeypatch):
    merge_sha = "a" * 40

    def command(*args, **kwargs):
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        if "cherry" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "rev-list" in args:
            return subprocess.CompletedProcess(args, 0, merge_sha + "\n", "")
        if "rev-parse" in args:
            # the tree-identity probe reads the tip tree; its merge-base call
            # then gets the scripted rc=1 above and the probe records norecord,
            # which must NOT decide the branch -- cherry and the merge scan do
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(D, "sh", command)

    unique, evidence = D._branch_work_against(
        ["git", "-C", "/unused"], "master", f"origin/{BRANCH}")

    assert unique is None
    assert evidence["outcome"] == "norecord"
    assert evidence["stage"] == "merge_parents"
    assert evidence["tree_identity"]["outcome"] == "norecord"


def test_clean_automatic_merge_with_carried_patches_is_already_carried(
        tmp_path):
    repo, merge = _already_carried_merge_graph(
        tmp_path, merge_only_change=False)

    unique, evidence = D._branch_work_against(
        ["git", "-C", str(repo)], "target", "topic")

    assert unique == []
    assert evidence["outcome"] == "complete"
    assert evidence["method"] == "patch_equivalence_with_automatic_merges"
    assert evidence["merge_proofs"] == [{
        "commit": merge,
        "parents_rc": 0,
        "merge_tree_rc": 0,
        "committed_tree_rc": 0,
        "recreated_tree": evidence["merge_proofs"][0]["committed_tree"],
        "committed_tree": evidence["merge_proofs"][0]["committed_tree"],
        "outcome": "complete",
        "method": "automatic_merge_tree",
    }]


def test_merge_only_content_remains_norecord_even_when_cherry_is_empty(
        tmp_path):
    repo, merge = _already_carried_merge_graph(
        tmp_path, merge_only_change=True)

    unique, evidence = D._branch_work_against(
        ["git", "-C", str(repo)], "target", "topic")

    assert unique is None
    assert evidence["outcome"] == "norecord"
    assert evidence["stage"] == "merge_tree_equivalence"
    assert evidence["merge_proofs"][0]["commit"] == merge
    assert (evidence["merge_proofs"][0]["recreated_tree"]
            != evidence["merge_proofs"][0]["committed_tree"])


# --------------------------------------------------------------------------
# A squash-merged branch: every byte on the mainline, invisible to git cherry
# --------------------------------------------------------------------------

def test_squash_merged_branch_is_already_carried_by_tree_identity(tmp_path):
    """OpenROAD `postroute-drv-repair`, 2026-09-07: four commits squash-merged
    as PR #14, tip tree identical to the squash commit's, and git cherry
    reporting all four as outstanding. Patch ids cannot see a squash; the
    tree can."""
    clone, _origin, source_tip = _graph(tmp_path, "squash")
    g = ["git", "-C", str(clone)]
    # THE CONTROL: cherry alone would call every branch commit outstanding.
    cherry = _git(clone, "cherry", "origin/master", f"origin/{BRANCH}").stdout
    assert [line[0] for line in cherry.strip().splitlines()] == ["+", "+"]
    assert _git(clone, "merge-base", "--is-ancestor", source_tip,
                "origin/master", check=False).returncode == 1

    unique, evidence = D._branch_work_against(g, "origin/master", f"origin/{BRANCH}")

    assert unique == []
    assert evidence["method"] == "tree_identity"
    carried_by = evidence["carried_by"]
    assert (_git(clone, "rev-parse", f"{carried_by}^{{tree}}").stdout.strip()
            == _git(clone, "rev-parse", f"{source_tip}^{{tree}}").stdout.strip())


def test_squash_merged_branch_forms_no_ai_case(tmp_path):
    """Step 2 must not merge it, must not conflict, must not hand it to the AI."""
    clone, _origin, _source_tip = _graph(tmp_path, "squash")
    rep = {}

    D.step2_ours(["git", "-C", str(clone)], "master", rep)

    assert rep["merged"] == []
    assert rep["conflicted"] == []
    assert "merge_unmeasurable" not in rep
    assert rep["carried_nothing_new"] == 1
    assert rep["ours_branch_evidence"][0]["outcome"] == "already_carried"
    assert rep["ours_branch_evidence"][0]["checks"][-1]["method"] == "tree_identity"


def test_a_declined_squash_case_verifies_by_tree_identity(tmp_path):
    """The AI's only correct verdict on such a case is DECLINE, and before this
    a DECLINE could never be verified, so the case stayed pending and blocked
    every release. Tree identity is its postcondition."""
    clone, _origin, source_tip = _graph(tmp_path, "squash")
    case = {"fork": "fixture", "branch": f"origin/{BRANCH}", "mainline": "master",
            "source_tip": source_tip, "path": str(clone)}

    proof = D._verify_ai_case(case)

    assert proof["verified"] is True, proof["reason"]
    assert "tree" in proof["reason"] and "squash" in proof["reason"]
    assert proof["tree_identity"]["carried_by"]


def test_an_absent_branch_is_still_outstanding_after_the_tree_probe(tmp_path):
    """THE BIDIRECTIONAL CONTROL: the tree probe must not turn genuinely absent
    work into "carried". The absent graph still forms a real case."""
    clone, _origin, source_tip = _graph(tmp_path, "absent")
    unique, evidence = D._branch_work_against(
        ["git", "-C", str(clone)], "origin/master", f"origin/{BRANCH}")
    assert unique == [source_tip]
    assert evidence["method"] == "outstanding_patches"
    assert evidence.get("tree_identity", {}).get("carried_by") is None
    case = {"fork": "fixture", "branch": f"origin/{BRANCH}", "mainline": "master",
            "source_tip": source_tip, "path": str(clone)}
    assert D._verify_ai_case(case)["verified"] is False
