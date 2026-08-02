"""vibeic-eda#53 follow-up — three historical pointers kept `--check` red on main.

`sync_image_version --check` exited 1 on `origin/main` for as long as anyone
looked, on three references that are PROSE and must not move:

    .image-version-ignore:7   the ignore file's OWN comment, quoting an example
    Dockerfile:719            "measured image-to-image against …:0.2.51"
    test_presence_default_image_tracks_version.py
                              the superseded DEFAULT_IMAGE the test exists to forbid

Rewriting any of them would state that a measurement was taken against a version
it was not taken against — the rule `.image-version-ignore` states in its own
header. But the ignore mechanism was PATH-GLOB only, so the only way to silence
them was to silence whole files: `Dockerfile`, which could later carry a live
pointer, and the test file, whose entire subject is that very drift.

So the opt-out became per LINE, written on the line it excuses, where a reader
of that line sees it.

A gate that has been red for months is a gate nobody reads, which is the same
failure as the nightly alarm in #61.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
_spec = importlib.util.spec_from_file_location(
    "sync_image_version", _ROOT / "sync_image_version.py")
S = importlib.util.module_from_spec(_spec)
sys.modules["sync_image_version"] = S
try:
    _spec.loader.exec_module(S)
except SystemExit:
    pass


def test_the_repo_passes_its_own_check():
    """THE POINT. Red on main before this; a gate nobody can satisfy is a gate
    nobody reads."""
    r = subprocess.run([sys.executable, str(_ROOT / "sync_image_version.py"),
                        "--check"], capture_output=True, text=True,
                       timeout=55, cwd=str(_ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_marked_line_is_skipped_and_an_unmarked_one_is_not(tmp_path):
    """Both directions, on a real git repo — `ghcr_hits` shells out to
    `git grep`, so a tmp dir without a repo would test nothing."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a],
                              capture_output=True, text=True, timeout=30)
    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (tmp_path / "live.md").write_text(
        "docker pull ghcr.io/vibeic/vibeic-eda:0.2.40\n", encoding="utf-8")
    (tmp_path / "hist.md").write_text(
        f"measured against ghcr.io/vibeic/vibeic-eda:0.2.31  "
        f"({S.HISTORY_LINE_MARK})\n", encoding="utf-8")
    git("add", "live.md", "hist.md")
    git("commit", "-q", "-m", "seed")
    hits = S.ghcr_hits(tmp_path, [])
    files = {rel for rel, _ln, _v in hits}
    assert "live.md" in files, hits
    assert "hist.md" not in files, hits


def test_the_marker_must_be_on_the_line_it_excuses(tmp_path):
    """LOAD-BEARING. A marker anywhere in the FILE would be a path-glob ignore
    with extra steps — and would silence a live pointer added later in the same
    file, which is the cost this exists to avoid."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a],
                              capture_output=True, text=True, timeout=30)
    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (tmp_path / "mixed.md").write_text(
        f"this file mentions {S.HISTORY_LINE_MARK} somewhere\n"
        "docker pull ghcr.io/vibeic/vibeic-eda:0.2.40\n", encoding="utf-8")
    git("add", "mixed.md")
    git("commit", "-q", "-m", "seed")
    hits = S.ghcr_hits(tmp_path, [])
    assert any(rel == "mixed.md" for rel, _l, _v in hits), (
        "a marker on another line silenced a live pointer")


def test_the_three_marked_lines_are_the_historical_ones():
    """Named, so a future reader can check the judgement rather than trust it.
    Each is a version quoted as the SUBJECT of a measurement or a forbidden
    value — never something a reader would copy and run."""
    for rel, needle in (
            (".image-version-ignore", "0.2.31"),
            ("Dockerfile", "0.2.51"),
            ("fork-gatekeeper/test_presence_default_image_tracks_version.py",
             "0.2.46")):
        p = _ROOT / rel
        if not p.is_file():
            continue
        marked = [l for l in p.read_text(encoding="utf-8").splitlines()
                  if needle in l and S.HISTORY_LINE_MARK in l]
        assert marked, f"{rel}: the {needle} reference is not marked"
