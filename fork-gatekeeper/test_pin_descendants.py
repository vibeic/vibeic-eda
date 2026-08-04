#!/usr/bin/env python3
"""tools/check_pin_descendants.py — the guard for vibeic-eda#86.

Every test here drives `main()` over a REAL git repository built in tmp_path,
so the exit code is produced the way the pre-push hook produces it. Only the two
network calls are substituted, because the question they answer (is B a
descendant of A) is GitHub's to answer and not what these tests are about.

The load-bearing tests are the last three. `check_pins_agree` already
established the convention this program follows — rc 2 means "checked nothing",
and it is NOT a pass — and the reason the convention exists is that this repo
has now been bitten three times by a program reporting a clean answer it never
computed. A crash inside a guard must not arrive as rc 1, because rc 1 here
means "a pin moved sideways", a statement about the TREE. A crash is a statement
about the PROGRAM.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_pin_descendants", ROOT / "tools" / "check_pin_descendants.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)                                    # type: ignore

OLD = "a" * 40
NEW = "b" * 40


def _git(repo: Path, *a: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *a],
                       capture_output=True, text=True, check=True)
    return p.stdout


def make_repo(tmp_path: Path, old: str, new: str, msg: str = "move the pin",
              name: str = "repo") -> Path:
    """A repo whose HEAD moves TOOL_REF from `old` to `new`."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    df = repo / "Dockerfile"
    df.write_text(f"FROM x\n# github.com/vibeic/toolrepo \nARG TOOL_REF={old}  # c\n")
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", "base")
    df.write_text(f"FROM x\n# github.com/vibeic/toolrepo \nARG TOOL_REF={new}  # c\n")
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", msg)
    return repo


def run(repo: Path, capsys=None) -> int:
    return mod.main(["--root", str(repo), "--base", "HEAD~1", "--head", "HEAD"])


def stub(monkeypatch, status, drops=()):
    monkeypatch.setattr(mod, "compare",
                        lambda r, o, n: {"status": status, "ahead_by": 1,
                                         "behind_by": len(drops)})
    monkeypatch.setattr(mod, "dropped_commits", lambda r, o, n: list(drops))


def test_descendant_move_is_clean(tmp_path, monkeypatch):
    stub(monkeypatch, "ahead")
    assert run(make_repo(tmp_path, OLD, NEW)) == 0


def test_sideways_without_declaration_is_a_finding(tmp_path, monkeypatch):
    stub(monkeypatch, "diverged", [("c" * 40, "the dropped one")])
    assert run(make_repo(tmp_path, OLD, NEW)) == 1


def test_sideways_names_what_it_drops(tmp_path, monkeypatch, capsys):
    """The noticing happens in the READING, so the subject must be printed."""
    stub(monkeypatch, "diverged", [("c" * 40, "Latin-Hypercube sampling")])
    run(make_repo(tmp_path, OLD, NEW))
    assert "Latin-Hypercube sampling" in capsys.readouterr().out


def test_correct_declaration_releases_the_gate(tmp_path, monkeypatch):
    stub(monkeypatch, "diverged", [("c" * 40, "x"), ("d" * 40, "y")])
    repo = make_repo(tmp_path, OLD, NEW, "move\n\nPIN-DROPS: toolrepo=2")
    assert run(repo) == 0


def test_declaration_with_the_wrong_count_does_not(tmp_path, monkeypatch):
    """A count you can write without looking would make the hatch decorative."""
    stub(monkeypatch, "diverged", [("c" * 40, "x"), ("d" * 40, "y")])
    repo = make_repo(tmp_path, OLD, NEW, "move\n\nPIN-DROPS: toolrepo=1")
    assert run(repo) == 1


def test_declaration_for_another_repo_does_not(tmp_path, monkeypatch):
    stub(monkeypatch, "diverged", [("c" * 40, "x")])
    repo = make_repo(tmp_path, OLD, NEW, "move\n\nPIN-DROPS: somethingelse=1")
    assert run(repo) == 1


def test_a_pin_file_touched_without_a_pin_moving_says_so(tmp_path, monkeypatch, capsys):
    """"Nothing to compare" and "everything is fine" must not read alike."""
    # A pin FILE changes, but no pin VALUE moves — the commonest real shape
    # (a comment, a build stage, a new ARG elsewhere).
    repo = tmp_path / "commentonly"
    repo.mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    df = repo / "Dockerfile"
    df.write_text(f"FROM x\n# github.com/vibeic/toolrepo \nARG TOOL_REF={OLD}  # c\n")
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", "base")
    df.write_text(df.read_text() + "# a comment, not a pin move\n")
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", "comment only")
    assert mod.main(["--root", str(repo), "--base", "HEAD~1", "--head", "HEAD"]) == 0
    assert "no ARG *_REF VALUE moved" in capsys.readouterr().out


def test_offline_is_not_reported_as_clean(tmp_path, monkeypatch, capsys):
    stub(monkeypatch, "diverged", [("c" * 40, "x")])
    repo = make_repo(tmp_path, OLD, NEW)
    mod.main(["--root", str(repo), "--base", "HEAD~1", "--head", "HEAD", "--offline"])
    assert "NOT a clean result" in capsys.readouterr().out


# --------------------------------------------------------------------------
# fail-closed. These are the ones that matter.
# --------------------------------------------------------------------------
def test_unanswerable_compare_is_rc2_never_rc0(tmp_path, monkeypatch):
    """A compare that could not be made is not evidence that nothing was lost."""
    def boom(*a, **k):
        raise mod.Undetermined("the network said no")
    monkeypatch.setattr(mod, "compare", boom)
    assert run(make_repo(tmp_path, OLD, NEW)) == 2


def test_an_unresolvable_repo_is_rc2_never_rc0(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "repo_for", lambda *a, **k: None)
    assert run(make_repo(tmp_path, OLD, NEW)) == 2


def test_a_crash_is_rc2_not_a_finding_about_the_tree(tmp_path, monkeypatch):
    """rc 1 means "a pin moved sideways" — a claim ABOUT THE TREE. A program
    that raises has made no such claim, and must not appear to."""
    def boom(*a, **k):
        raise RuntimeError("this is a bug in the guard, not a bad pin")
    monkeypatch.setattr(mod, "compare", boom)
    with pytest.raises(RuntimeError):
        run(make_repo(tmp_path, OLD, NEW, name="a"))
    # ...and the module's __main__ guard converts that into rc 2, not rc 1.
    repo = make_repo(tmp_path, OLD, NEW, name="b")
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_pin_descendants.py"),
         "--root", str(repo), "--base", "HEAD~1", "--head", "HEAD"],
        capture_output=True, text=True,
        env={"PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
