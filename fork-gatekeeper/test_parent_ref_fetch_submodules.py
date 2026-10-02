"""Experimental gitlinks must not prevent measuring the published parent ref."""
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import daily_0530 as D
import daily_merge as M


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo, message):
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", message, "--allow-empty")


@pytest.fixture
def parent_with_unpublished_gitlink(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    dependency = tmp_path / "dependency"
    git(tmp_path, "init", "-q", "-b", "main", str(dependency))
    commit(dependency, "published dependency")
    published = tmp_path / "published.git"
    git(tmp_path, "clone", "-q", "--bare", str(dependency), str(published))
    commit(dependency, "unpublished experimental dependency")
    unpublished = git(dependency, "rev-parse", "HEAD")

    parent = tmp_path / "parent"
    git(tmp_path, "init", "-q", "-b", "main", str(parent))
    git(parent, "submodule", "add", "-q", str(published), "dep")
    commit(parent, "parent with published dependency")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--recurse-submodules", str(parent), str(clone))
    git(clone, "remote", "add", "upstream", str(parent))
    git(clone, "fetch", "--no-recurse-submodules", "-q", "upstream")
    git(clone, "config", "fetch.recurseSubmodules", "on-demand")

    git(parent, "checkout", "-qb", "experimental")
    git(parent, "update-index", "--cacheinfo", "160000", unpublished, "dep")
    commit(parent, "experimental branch references an unpublished gitlink")
    git(parent, "checkout", "-q", "main")
    return parent, clone


@pytest.mark.parametrize("moved_main", [False, True])
@pytest.mark.parametrize("entry", ["upstream", "origin", "daily_merge"])
def test_parent_comparisons_ignore_unpublished_experimental_submodule(
        parent_with_unpublished_gitlink, monkeypatch, entry, moved_main):
    parent, clone = parent_with_unpublished_gitlink
    if moved_main:
        commit(parent, "new published parent commit")
    published_tip = git(parent, "rev-parse", "main")
    before_dependency = git(clone / "dep", "rev-parse", "HEAD")

    if entry == "upstream":
        rep = {}
        D.step1_upstream(("git", "-C", str(clone)), "main", rep)
        expected = "merged 1 upstream commit(s)" if moved_main else "already current"
        assert rep["upstream"] == expected, rep
        assert rep["upstream_evidence"]["tip_seen"] == published_tip
    elif entry == "origin":
        branches = D._fork_branches(("git", "-C", str(clone)), "main")
        assert branches == ["origin/experimental"], branches
        assert git(clone, "rev-parse", "origin/main") == published_tip
    else:
        monkeypatch.setattr(M, "FORKS_ROOT", clone.parent)
        result = M.merge_one("clone", "main", dry=True, checks=[])
        # This fixture's origin and upstream share the published parent, so
        # refreshing origin also carries the new main commit already.
        assert result["state"] == "ALREADY_CURRENT", result
        assert result["upstream_default"]["remote_tip"] == published_tip

    assert git(clone / "dep", "rev-parse", "HEAD") == before_dependency


@pytest.mark.parametrize("diagnostic", [
    "fatal: remote error: upload-pack: not our ref deadbeef",
    "error: cannot lock ref refs/remotes/upstream/work",
    "remote: error: permission denied",
])
def test_fetch_diagnosis_survives_progress_output(diagnostic):
    cp = subprocess.CompletedProcess(
        ["git", "fetch"], 1, "",
        "remote: Enumerating objects: 450, done.\n"
        "Receiving objects: 100% (450/450), done.\n" + diagnostic + "\n"
    )
    assert D._first_error_line(cp) == diagnostic
