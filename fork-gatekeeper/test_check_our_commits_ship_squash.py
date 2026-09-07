"""check_our_commits_ship: a squash-merged branch is not stranded work.

Real git repositories, not mocks: a two-commit branch squash-merged into the
shipped line, then the line moving on. `git cherry` marks both branch commits
`+` (a squash's single patch is the sum of two); the branch tip's tree is on
the pin's history, byte for byte. Measured 2026-09-07 on OpenROAD: 7 such
commits reported stranded the morning after 0.3.47 shipped, tick exit 6.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_our_commits_ship as S  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
    return r.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Reyer", "-c", "user.email=reyerchu@example.invalid",
         "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, *, squash: bool) -> tuple[Path, str]:
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=master")
    (repo / "a.txt").write_text("base\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "topic")
    (repo / "a.txt").write_text("topic 1\n")
    _commit(repo, "drt: first of two")
    (repo / "b.txt").write_text("topic 2\n")
    _commit(repo, "drt: second of two")
    _git(repo, "checkout", "-q", "master")
    if squash:
        _git(repo, "merge", "-q", "--squash", "topic")
        _commit(repo, "squash of topic (a merged PR)")
    (repo / "c.txt").write_text("later\n")
    pin = _commit(repo, "later mainline work")
    return repo, pin


def test_a_squash_merged_branch_is_not_stranded(tmp_path, monkeypatch):
    """THE REGRESSION. Both topic commits read `+` to git cherry; their tree is
    on the pin's history; nothing is stranded."""
    repo, pin = _repo(tmp_path, squash=True)
    monkeypatch.setattr(S, "OURS", ("reyer",))
    cherry = _git(repo, "cherry", pin, "topic")
    assert [ln[0] for ln in cherry.splitlines()] == ["+", "+"], cherry   # the control inside
    assert S.stranded(repo, pin) == []


def test_an_unmerged_branch_is_still_stranded(tmp_path, monkeypatch):
    """THE BIDIRECTIONAL CONTROL: without the squash, the same two commits are
    exactly what this program exists to report."""
    repo, pin = _repo(tmp_path, squash=False)
    monkeypatch.setattr(S, "OURS", ("reyer",))
    rows = S.stranded(repo, pin)
    assert sorted(r["subject"] for r in rows) == ["drt: first of two", "drt: second of two"]
    assert all(r["branch"] == "topic" for r in rows)


def test_a_probe_that_cannot_answer_hides_nothing(tmp_path, monkeypatch):
    """If the tree probe is unavailable, the answer is 'not carried', and the
    stranded report stands -- unknown never becomes silence."""
    repo, pin = _repo(tmp_path, squash=True)
    monkeypatch.setattr(S, "OURS", ("reyer",))
    monkeypatch.setattr(S, "_squash_carried", lambda *a: False, raising=False)
    assert len(S.stranded(repo, pin)) == 2
