#!/usr/bin/env python3
"""The 05:30 merger must mutate the same branch the image release advances."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("daily_0530", HERE / "daily_0530.py")
D = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(D)

PUBLISHED = "vibeic/xyce-trilinos-17.2-epetra-restored"


def _git(repo: Path, *args: str, check: bool = True):
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                        text=True)
    if check and cp.returncode:
        raise AssertionError(cp.stderr or cp.stdout)
    return cp


def _commit(repo: Path, subject: str, body: str) -> str:
    path = repo / "state.txt"
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _graph(tmp_path: Path):
    origin = tmp_path / "origin.git"
    publisher = tmp_path / "publisher"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "--initial-branch=master",
         str(origin))
    _git(tmp_path, "init", "-q", "--initial-branch=master", str(publisher))
    for repo in (publisher,):
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "test")
    base = _commit(publisher, "base", "base\n")
    _git(publisher, "remote", "add", "origin", str(origin))
    _git(publisher, "push", "-q", "-u", "origin", "master")
    _git(publisher, "checkout", "-q", "-b", PUBLISHED, base)
    _git(publisher, "push", "-q", "-u", "origin", PUBLISHED)

    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.invalid")
    _git(clone, "config", "user.name", "test")
    _git(clone, "checkout", "-q", "--track", "-b", PUBLISHED,
         f"origin/{PUBLISHED}")

    remote_tip = _commit(publisher, "upstream arrived", "remote\n")
    _git(publisher, "push", "-q", "origin", PUBLISHED)
    _git(clone, "fetch", "-q", "origin", "--prune")
    _git(clone, "checkout", "-q", "master")
    return clone, publisher, remote_tip


def test_declared_slash_branch_wins_over_existing_master_and_fast_forwards(
        tmp_path, monkeypatch):
    clone, _publisher, remote_tip = _graph(tmp_path)
    monkeypatch.setattr(D, "declared_branch", lambda _repo: PUBLISHED)
    g = ["git", "-C", str(clone)]

    assert D.published_mainline(g, "Trilinos") == PUBLISHED
    ok, evidence = D.checkout_published_line(g, PUBLISHED)

    assert ok is True
    assert evidence["outcome"] == "complete"
    assert evidence["local_tip_before"] != remote_tip
    assert evidence["local_tip_after"] == remote_tip
    assert _git(clone, "branch", "--show-current").stdout.strip() == PUBLISHED
    # The published line is not a topic branch merely because its name contains
    # a slash, and therefore must never be offered to consolidation or pruning.
    assert f"origin/{PUBLISHED}" not in D._fork_branches(g, PUBLISHED)


def test_a_diverged_local_published_line_is_retained_not_reset(
        tmp_path, monkeypatch):
    clone, publisher, _remote_tip = _graph(tmp_path)
    monkeypatch.setattr(D, "declared_branch", lambda _repo: PUBLISHED)
    _git(clone, "checkout", "-q", PUBLISHED)
    local_tip = _commit(clone, "unpublished local work", "local\n")
    remote_tip = _commit(publisher, "remote moved again", "remote again\n")
    _git(publisher, "push", "-q", "origin", PUBLISHED)
    _git(clone, "fetch", "-q", "origin", "--prune")

    ok, evidence = D.checkout_published_line(
        ["git", "-C", str(clone)], PUBLISHED)

    assert ok is False
    assert evidence["outcome"] == "diverged"
    assert evidence["local_tip_after"] == local_tip
    assert _git(clone, "rev-parse", f"origin/{PUBLISHED}").stdout.strip() == remote_tip
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == local_tip


def test_missing_declared_branch_does_not_fall_back_to_master(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--initial-branch=master", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _commit(repo, "base", "base\n")
    monkeypatch.setattr(D, "declared_branch", lambda _repo: PUBLISHED)

    assert D.published_mainline(
        ["git", "-C", str(repo)], "Trilinos") is None
