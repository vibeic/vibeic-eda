"""`tree_basis` must say whether it could establish the tree's currency.

WHAT HAPPENED, on 2026-08-01, twice.

The first time, `check_pins_current` reported `OpenROAD b6fd2b2fe STALE — 1 of
OUR merged commit(s) not shipping` from a checkout 15 commits behind
`origin/main`, which carried `47636465f9` and had shipped it as 0.2.53. Acting
on that report means rebuilding the image at a pin many commits OLDER than the
one already published — a regression, driven by a gate. `tree_basis` was added
for exactly that and states the HEAD.

The second time is what this file is about. The remedy for a dirty shared
checkout is to run the check from a DETACHED worktree pinned at `origin/main` —
and `@{u}` is undefined on a detached HEAD, so `behind` came back `None`, and
the printer's `if basis.get("behind")` is false for `None` and for `0` alike.
The tree whose currency we most wanted stated printed nothing about it, exactly
like a tree that had been confirmed current.

So: three outcomes, three sentences. Behind by N (names the ref), up to date
with a named ref, or COULD NOT TELL. Never silence.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent


# PLAIN IMPORT, deliberately. The first version of this file loaded the module
# by path and REPLACED `sys.modules["check_pins_current"]` with a fresh copy —
# which broke `test_daily_release`'s
# `assert R.branch_is_ours is C.branch_is_ours`, an IDENTITY check that exists
# because two copies of that answer are how the two programs came to disagree
# about four pins. A second module object is exactly the thing it forbids, and a
# test that manufactures one is defeating a real invariant to import a file.
sys.path.insert(0, str(_HERE))
import check_pins_current as M  # noqa: E402


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True, timeout=60)


def _repo(tmp_path, name="up"):
    """A real origin + a real clone. The behaviour under test is git's, so a
    fake would only prove the fake."""
    up = tmp_path / name
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@t")
    _git(up, "config", "user.name", "t")
    (up / "VERSION").write_text("0.0.1\n")
    _git(up, "add", "VERSION")
    _git(up, "commit", "-qm", "one")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)],
                   capture_output=True, timeout=120)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return up, clone


def _advance(up, clone, n=3):
    for i in range(n):
        (up / f"f{i}").write_text("x")
        _git(up, "add", f"f{i}")
        _git(up, "commit", "-qm", f"c{i}")
    _git(clone, "fetch", "-q", "origin")


def test_a_current_tracking_branch_reports_zero_against_a_named_ref(tmp_path):
    _up, clone = _repo(tmp_path)
    b = M.tree_basis(clone)
    assert b["behind"] == 0
    assert b["behind_basis"] is not None


def test_a_behind_tree_reports_the_distance(tmp_path):
    up, clone = _repo(tmp_path)
    _advance(up, clone, 3)
    b = M.tree_basis(clone)
    assert b["behind"] == 3, b


def test_a_detached_worktree_still_gets_an_answer(tmp_path):
    """THE SECOND INCIDENT. `@{u}` is undefined here, and this is the RECOMMENDED
    way to run the check when the shared checkout is dirty."""
    up, clone = _repo(tmp_path)
    _advance(up, clone, 2)
    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "--detach", str(wt), "HEAD")
    b = M.tree_basis(wt)
    assert b["behind"] == 2, b
    assert b["behind_basis"] in ("origin/main", "origin/master"), b


def test_an_unknowable_currency_is_not_reported_as_zero(tmp_path):
    """A repo with no remote at all. `behind` must be None — distinguishable
    from 0 — so the printer can say COULD NOT TELL instead of nothing."""
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(solo, "init", "-q", "-b", "main")
    _git(solo, "config", "user.email", "t@t")
    _git(solo, "config", "user.name", "t")
    (solo / "a").write_text("x")
    _git(solo, "add", "a")
    _git(solo, "commit", "-qm", "one")
    b = M.tree_basis(solo)
    assert b["behind"] is None, b
    assert b["head"], b


def test_a_non_repo_says_so(tmp_path):
    b = M.tree_basis(tmp_path)
    assert b["head"] is None
    assert "unknown" in b["note"]


def test_a_dirty_pin_file_is_named_in_full(tmp_path):
    """`--porcelain` is `XY<space>PATH`; a fixed slice ate the first character
    and produced `ockerfile`, which a reader cannot act on."""
    _up, clone = _repo(tmp_path)
    (clone / "Dockerfile").write_text("FROM scratch\n")
    _git(clone, "add", "Dockerfile")
    b = M.tree_basis(clone)
    assert "Dockerfile" in b["dirty_pin_files"], b


# ── the printed sentence, which is what a reader actually acts on ───────────
def _run_in(tree):
    r = subprocess.run([sys.executable, str(_HERE / "check_pins_current.py"),
                        "--eda-root", str(tree)],
                       capture_output=True, text=True, timeout=300)
    return r.stdout + r.stderr


def test_the_report_states_a_behind_tree(tmp_path):
    up, clone = _repo(tmp_path)
    _advance(up, clone, 4)
    out = _run_in(clone)
    assert "4 commit(s) BEHIND" in out, out


def test_the_report_never_stays_silent_about_an_unknown_currency(tmp_path):
    """LOAD-BEARING, and the whole point: silence reads as "current" to the one
    reader who most needs it not to."""
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(solo, "init", "-q", "-b", "main")
    _git(solo, "config", "user.email", "t@t")
    _git(solo, "config", "user.name", "t")
    (solo / "a").write_text("x")
    _git(solo, "add", "a")
    _git(solo, "commit", "-qm", "one")
    out = _run_in(solo)
    assert "could NOT establish" in out, out
    assert "UNKNOWN, not confirmed" in out, out


def test_a_current_tree_says_so_rather_than_saying_nothing(tmp_path):
    _up, clone = _repo(tmp_path)
    out = _run_in(clone)
    assert "up to date with" in out, out
