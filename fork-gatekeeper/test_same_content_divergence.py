"""vibeic-eda#61 — a nightly push that could never succeed.

`FasterCap` failed every night from 2026-08-01 and would have kept failing
forever. One change, committed twice: same subject, same date, and the same
TREE, differing only in author metadata — because the mirror was created by
clone+push (the fork API returns 403) and the same patch was later applied in a
clone with no shared history.

    local  HEAD^{tree}          76f4e291e08d
    remote origin/master^{tree} 76f4e291e08d
    ahead 1 / behind 1          STRUCTURAL — retrying reproduces it

Two behaviours land, and both are DERIVED from the two tree hashes rather than
from a list of mirrors to keep up to date:

  * equal trees, different shas  -> ADOPT the remote, do not push. With equal
    trees there is nothing of OURS to publish, only a different spelling of the
    same content, so adopting discards nothing.
  * genuinely diverged           -> DIVERGED, not `REJECTED`. A divergence is
    not a lost race, and only one of the two is worth waking someone for.
    Rendering both the same way is how a nightly alarm that fires every single
    night stops being read.

Driven on REAL git repositories built in a tmp dir, because the predicate is
about what `git rev-parse <ref>^{tree}` returns and a stubbed `out()` would test
the stub.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("d0530", _HERE / "daily_0530.py")
D = importlib.util.module_from_spec(_spec)
sys.modules["d0530"] = D
_spec.loader.exec_module(D)


def _git(repo, *a, env=None):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                          text=True, timeout=30, env=env)


def _seed(repo: pathlib.Path):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "master", ".")
    _git(repo, "config", "user.email", "a@a")
    _git(repo, "config", "user.name", "A")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")


def _pair(tmp_path, same_content: bool):
    """A clone whose `master` and `origin/master` differ. When `same_content`,
    the two commits carry the identical tree — the #61 shape."""
    up = tmp_path / "up"
    _seed(up)
    (up / "f.txt").write_text("two\n", encoding="utf-8")
    _git(up, "add", "f.txt")
    _git(up, "commit", "-q", "-m", "the change")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], timeout=60)
    _git(clone, "config", "user.email", "b@b")
    _git(clone, "config", "user.name", "B")
    # Rewind and re-apply, so the clone's commit is a DIFFERENT sha.
    _git(clone, "reset", "--hard", "HEAD~1")
    (clone / "f.txt").write_text("two\n" if same_content else "three\n",
                                 encoding="utf-8")
    _git(clone, "add", "f.txt")
    _git(clone, "commit", "-q", "-m", "the change, again")
    return clone


def test_equal_trees_under_different_shas_are_detected(tmp_path):
    """THE DEFECT's shape. The remote sha is returned so the caller can adopt
    it."""
    clone = _pair(tmp_path, same_content=True)
    g = ["git", "-C", str(clone)]
    got = D.same_content_divergence(g, "master")
    assert got, "the same-tree divergence was not detected"
    assert got == _git(clone, "rev-parse", "origin/master").stdout.strip()


def test_a_REAL_divergence_is_not_treated_as_the_same_content(tmp_path):
    """LOAD-BEARING. Adopting the remote here would DISCARD our change. The
    whole safety of the adopt path rests on this returning None."""
    clone = _pair(tmp_path, same_content=False)
    g = ["git", "-C", str(clone)]
    assert D.same_content_divergence(g, "master") is None


def test_an_identical_head_is_not_a_divergence(tmp_path):
    """Nothing to adopt when the shas already agree — returning a sha here
    would make every clean fork take the reset path."""
    up = tmp_path / "up"
    _seed(up)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], timeout=60)
    g = ["git", "-C", str(clone)]
    assert D.same_content_divergence(g, "master") is None


def test_an_unresolvable_ref_is_None_not_a_false_match(tmp_path):
    """Two empty strings compare equal. If a failed `rev-parse` were allowed
    through, every repo without an `origin/<main>` would look like a same-tree
    divergence and get reset to a ref that does not exist."""
    up = tmp_path / "up"
    _seed(up)
    g = ["git", "-C", str(up)]          # no origin at all
    assert D.same_content_divergence(g, "master") is None


def test_the_predicate_is_derived_from_trees_not_from_a_mirror_list():
    """A hardcoded `if tool == "FasterCap"` would fix tonight and reproduce the
    defect on the next mirror created the same way."""
    src = (_HERE / "daily_0530.py").read_text(encoding="utf-8")
    seg = src[src.index("def same_content_divergence"):]
    seg = seg[:seg.index("\ndef ", 10)]
    # The BODY, taken after the closing docstring quotes. Stripping lines that
    # merely START with a quote leaves the docstring's continuation lines in —
    # and the docstring names FasterCap while explaining the defect, so the
    # first version of this assertion failed on its own documentation.
    body = seg[seg.index('"""', seg.index('"""') + 3) + 3:]
    assert "FasterCap" not in body, body
    # `^{{tree}}` — doubled, because the call sites are f-strings. Looking for
    # the single-brace form found nothing and reddened a correct predicate.
    assert "^{{tree}}" in body


def test_a_divergence_and_a_rejection_are_different_words():
    """Only one of the two is worth waking someone for."""
    src = (_HERE / "daily_0530.py").read_text(encoding="utf-8")
    assert "non-fast-forward" in src
    assert 'rep["push"] = f"REJECTED:' in src
    assert "DIVERGED: our" in src
