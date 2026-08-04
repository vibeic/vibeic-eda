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
    # The fixture strings carry the marker themselves (image-version:history):
    # they are TEST DATA, and without it this file's own examples are scanned as
    # live pointers — which is precisely what happened, and only after `git add`,
    # because `ghcr_hits` shells out to `git grep` and cannot see an untracked
    # file. Verified before staging, so the check passed on a file the scanner
    # could not yet read.
    _LIVE = "docker pull ghcr.io/vibeic/" + "vibeic-eda:0.2.40"
    _HIST = "measured against ghcr.io/vibeic/" + "vibeic-eda:0.2.31"
    (tmp_path / "live.md").write_text(_LIVE + "\n", encoding="utf-8")
    (tmp_path / "hist.md").write_text(
        f"{_HIST}  ({S.HISTORY_LINE_MARK})\n", encoding="utf-8")
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
    _LIVE = "docker pull ghcr.io/vibeic/" + "vibeic-eda:0.2.40"
    (tmp_path / "mixed.md").write_text(
        f"this file mentions {S.HISTORY_LINE_MARK} somewhere\n"
        + _LIVE + "\n", encoding="utf-8")
    git("add", "mixed.md")
    git("commit", "-q", "-m", "seed")
    hits = S.ghcr_hits(tmp_path, [])
    assert any(rel == "mixed.md" for rel, _l, _v in hits), (
        "a marker on another line silenced a live pointer")


def test_each_historical_reference_is_excused_at_the_right_level():
    """Named, so a future reader can check the judgement rather than trust it —
    and the LEVEL differs by file, for a reason that was measured the hard way.

    INLINE, because the file is not an image input:
        .image-version-ignore                the ignore file's own example
        test_presence_default_image_tracks_version.py
                                             the superseded DEFAULT_IMAGE it forbids

    PATH, because the file MAY NOT BE TOUCHED:
        Dockerfile                           an input to the image

    `compose_recipe_hash` digests the root Dockerfile whole, comments included,
    deliberately. Adding an inline marker there invalidated the 0.2.56 release
    record — measured: the fingerprint stopped reproducing (c2b9b8b9a843 against
    3a0b5d567c32) and `check_release_recorded` failed. A file that may not be
    edited cannot carry an inline opt-out, so its exemption has to be the path.
    """
    inline = {".image-version-ignore": "0.2.31",
              "fork-gatekeeper/test_presence_default_image_tracks_version.py":
                  "0.2.46"}
    for rel, needle in inline.items():
        p = _ROOT / rel
        if not p.is_file():
            continue
        marked = [l for l in p.read_text(encoding="utf-8").splitlines()
                  if needle in l and S.HISTORY_LINE_MARK in l]
        assert marked, f"{rel}: the {needle} reference is not marked inline"
    ig = (_ROOT / ".image-version-ignore")
    if ig.is_file():
        entries = [l.strip() for l in ig.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.strip().startswith("#")]
        assert "Dockerfile" in entries, (
            "the root Dockerfile must be excused at PATH level — it is an image "
            "input, and editing it to add a marker breaks the release record")


def test_no_bookkeeping_marker_was_written_into_the_root_Dockerfile():
    """THE REGRESSION THIS FILE CAUSED, pinned so it cannot recur. A bookkeeping
    edit to an image input is not a bookkeeping edit.

    IT ASSERTS THE RULE, NOT A SNAPSHOT. This froze `sha256(Dockerfile)` against
    a hardcoded commit, `83b8eff`. That holds only until the Dockerfile changes
    for a REAL reason — a pin advance, or the provenance collector added for #77
    (127 -> 111 layers) — which is to say, until the next release. It had been
    failing for exactly that reason, and a red test is a test nobody reads.

    The rule it means is narrower than "this file must never change": do not
    excuse the Dockerfile from the drift-net by writing a marker INTO it, because
    the file is an image input and the edit changes what ships. The correct
    excuse is the PATH-level entry in `.image-version-ignore`, which the test
    above already requires. So the invariant is: that marker must not appear
    here.

    Reproducibility is a different question and already has an owner —
    `check_release_recorded` compares the tree's fingerprint against
    RELEASED.json, which is where a moved input legitimately shows up (as
    PINS_AHEAD or as UNREPRODUCIBLE). Nothing is lost by dropping the snapshot;
    a frozen byte-comparison was answering that question by proxy, and stopped.
    """
    p = _ROOT / "Dockerfile"
    if not p.is_file():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    assert S.HISTORY_LINE_MARK not in text, (
        f"the root Dockerfile carries the {S.HISTORY_LINE_MARK!r} marker. That "
        f"is a bookkeeping edit to an image input: it changes what the build "
        f"consumes so RELEASED.json stops reproducing. Excuse it at PATH level "
        f"in .image-version-ignore instead — the entry is already there.")
