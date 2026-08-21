"""A release that builds and cannot publish must not stop every later round.

The pins are written BEFORE the build and committed only after the image is in a
registry, so a lost upload leaves them uncommitted. The page-publication tests
are integration tests that check the REAL repository at the publication
boundary, so a dirty tree turns 5 of them red -> the semantic pytest gate is RED
-> run_tick is NOT CHECKED -> the release never retries -> the pins stay dirty.

MEASURED 2026-08-21: round 11 built 0.3.20 with "gates: 10/10" and "smoke: all
11 tools run", lost the upload to `net/http: timeout awaiting response headers`,
and round 12 died on exactly those 5 tests without reaching the retry. A
deadlock that needs a human after every network hiccup is not a flow.

`recover_pin_only_dirty_tree` closes it at the start of a round — and the whole
value is in what it REFUSES, so that is what most of this file tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import recover_pin_only_dirty_tree as R  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _dockerfile(sha: str) -> str:
    return (f"FROM scratch\n"
            f"# github.com/vibeic/OpenROAD\n"
            f"ARG OPENROAD_REF={sha}\n")


def _repo(tmp_path: Path) -> Path:
    d = tmp_path / "eda"
    (d / "tools" / "openroad").mkdir(parents=True)
    (d / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_A))
    (d / "README.md").write_text("not a pin file\n")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


def _advance_pin(repo: Path) -> None:
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))


def _run(repo: Path, *extra: str) -> int:
    return R.main(["--eda-root", str(repo), *extra])


def _dirty(repo: Path) -> list[str]:
    return [x for x in _git(repo, "status", "--porcelain").splitlines() if x]


def _head_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


# --- the deadlock it exists to break ----------------------------------------

def test_a_pin_only_dirty_tree_is_committed_and_the_round_can_proceed(tmp_path):
    repo = _repo(tmp_path)
    _advance_pin(repo)
    before = _head_count(repo)
    assert _dirty(repo), "precondition: the tree must be dirty"

    assert _run(repo) == R.RC_OK
    assert _dirty(repo) == [], "the tree the next round sees is still dirty"
    assert _head_count(repo) == before + 1
    assert R.COMMIT_SUBJECT in _git(repo, "log", "-1", "--format=%s")


def test_a_clean_tree_is_a_pass_and_not_an_empty_commit(tmp_path):
    repo = _repo(tmp_path)
    before = _head_count(repo)
    assert _run(repo) == R.RC_OK
    assert _head_count(repo) == before, "it invented a commit with nothing to do"


# --- what it must REFUSE ----------------------------------------------------

def test_a_source_edit_riding_along_is_refused(tmp_path):
    """The release commit's own fear, kept: sweeping in an edit nobody reviewed.
    A human mid-change must not have their work committed by a cron round."""
    repo = _repo(tmp_path)
    _advance_pin(repo)
    (repo / "README.md").write_text("someone is editing this\n")

    assert _run(repo) == R.RC_REFUSED
    assert len(_dirty(repo)) == 2, "it committed something after refusing"


def test_an_untracked_file_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _advance_pin(repo)
    (repo / "notes.txt").write_text("scratch\n")
    assert _run(repo) == R.RC_REFUSED
    assert len(_dirty(repo)) == 2


def test_an_already_staged_change_is_refused(tmp_path):
    """A staged entry is someone mid-commit. Their index is not ours to finish."""
    repo = _repo(tmp_path)
    _advance_pin(repo)
    _git(repo, "add", "tools/openroad/Dockerfile")
    assert _run(repo) == R.RC_REFUSED
    assert _dirty(repo), "the staged change was consumed"


def test_a_deleted_pin_file_is_refused(tmp_path):
    """Deleting a pin file is not advancing a pin."""
    repo = _repo(tmp_path)
    (repo / "tools" / "openroad" / "Dockerfile").unlink()
    assert _run(repo) == R.RC_REFUSED


def test_a_non_pin_line_inside_a_pin_file_is_refused(tmp_path):
    """The check is on the DIFF, not the filename. A pin file carrying an edit
    that is not pin-shaped is still someone else's work."""
    repo = _repo(tmp_path)
    (repo / "tools" / "openroad" / "Dockerfile").write_text(
        _dockerfile(SHA_B) + "RUN echo this is not a pin\n")
    assert _run(repo) == R.RC_REFUSED
    assert _dirty(repo)


def test_dry_run_reports_without_committing(tmp_path):
    repo = _repo(tmp_path)
    _advance_pin(repo)
    before = _head_count(repo)
    assert _run(repo, "--dry-run") == R.RC_OK
    assert _head_count(repo) == before
    assert _dirty(repo), "--dry-run committed"


def test_a_refusal_is_loud_and_names_the_path(tmp_path, capsys):
    """A guard that degrades to 'nothing to do' would report success while the
    deadlock stayed. Refusing must say which path stopped it."""
    repo = _repo(tmp_path)
    _advance_pin(repo)
    (repo / "notes.txt").write_text("scratch\n")
    _run(repo)
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "notes.txt" in out
