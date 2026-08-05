"""The commit that records a release must contain the pins it was built from.

WHY THIS EXISTS (measured 2026-08-05)
====================================
The run that published `0.2.65` moved `OPENROAD_REF` from `f396ce8ee` to
`b64a496b9`, composed the image from the moved pin, pushed it, and then
committed only:

    README.md     | 12 ++++++------
    RELEASED.json | 22 +++++++++++-----------
    VERSION       |  2 +-

So `HEAD` recorded `f396ce8ee` while the published image was built from
`b64a496b9`, and `Dockerfile`, `docker-bake.hcl` and `tools/openroad/Dockerfile`
sat modified-but-uncommitted in the working tree. Anyone cloning `main` got pins
that do not describe the image the very same commit says was released.

That is precisely the question `RELEASED.json` exists to answer -- its own
docstring says "What the last PUBLISHED image was built from" -- so the record
was answering it with a tree that had already moved on.

WHY IT WAS INVISIBLE
--------------------
Every check in this repo that reads pins reads the WORKING TREE, and the working
tree was correct. `check_pins_agree` rc=0, `check_pin_descendants` rc=0,
`check_pinned_images_exist` rc=0 -- all true, all measuring a state that was
never committed. The one question nobody asked was whether the state they agreed
on had been *recorded*. Same family as vibeic-eda#94: the declaration is checked,
the thing the declaration is supposed to survive into is not.

WHAT THIS ASSERTS
-----------------
That `commit_release_record` names the pin sites at all. It cannot assert the
richer property -- "the commit contains exactly the pins the image was built
from" -- without running a release, so it asserts the necessary condition that
was missing, and states plainly that it is the necessary one.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
import daily_release as dr  # noqa: E402


def _record_file_list_source() -> str:
    src = (_HERE / "daily_release.py").read_text()
    i = src.index("def commit_release_record(")
    j = src.index("\ndef ", i + 1)
    return src[i:j]


def test_the_record_names_the_pin_sites():
    body = _record_file_list_source()
    for needed in ('"Dockerfile"', '"docker-bake.hcl"', '"tools"'):
        assert needed in body, (
            f"commit_release_record does not name {needed}, so a release that "
            f"moves a pin commits VERSION/RELEASED.json/README.md and leaves "
            f"the pin uncommitted. HEAD then records a different pin from the "
            f"one the published image was built from — which is the single "
            f"question RELEASED.json exists to answer.")


def test_the_record_still_names_the_version_files():
    """Control: a change that swapped one list for another rather than
    extending it would pass the test above while losing the original point."""
    body = _record_file_list_source()
    for needed in ('"VERSION"', '"RELEASED.json"', '"README.md"'):
        assert needed in body, f"commit_release_record no longer names {needed}"


def test_it_never_stages_everything():
    """`git add -A` in a release would sweep in whatever else is in the tree —
    including, on this machine, other agents' in-progress files."""
    body = _record_file_list_source()
    assert '"-A"' not in body and "'-A'" not in body, (
        "commit_release_record stages everything; the file list exists so a "
        "release commit contains the release and nothing else")
    assert '"--"' in body, "the git invocations no longer use `--` to end options"


def test_the_named_pin_sites_are_the_ones_check_pins_agree_reads():
    """The record and the checker must cover the same files, or the record can
    be complete by its own definition and still omit a pin the checker polices.

    `rewrite_pin` is the authority: it is what a release actually edits.
    """
    src = (_HERE / "daily_release.py").read_text()
    i = src.index("def rewrite_pin(")
    j = src.index("\ndef ", i + 1)
    rewrite = src[i:j]

    # rewrite_pin edits tools/*/Dockerfile, the root Dockerfile and docker-bake.hcl
    assert '"tools"' in rewrite and '"Dockerfile"' in rewrite and '"docker-bake.hcl"' in rewrite, (
        "rewrite_pin no longer edits the file set this test assumes; re-derive "
        "the record's file list from it rather than trusting this assertion")

    body = _record_file_list_source()
    for needed in ('"Dockerfile"', '"docker-bake.hcl"', '"tools"'):
        assert needed in body, (
            f"rewrite_pin edits {needed} but commit_release_record does not "
            f"record it")


def test_a_release_that_moved_no_pin_still_commits_cleanly(tmp_path):
    """Naming more files must not make an ordinary release fail.

    `git status --porcelain -- <paths>` filters to what actually changed, so
    listing every tool Dockerfile is safe. Proven rather than argued, because
    "it is filtered later" is exactly the kind of claim that turns out to be
    about a different code path.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "VERSION").write_text("0.0.1\n")
    (repo / "RELEASED.json").write_text("{}\n")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / "docker-bake.hcl").write_text("# bake\n")
    (repo / "tools" / "widget").mkdir(parents=True)
    (repo / "tools" / "widget" / "Dockerfile").write_text("FROM scratch\n")
    run("add", "-A")
    run("commit", "-qm", "base")

    # Only VERSION moves — no pin touched.
    (repo / "VERSION").write_text("0.0.2\n")
    ok, note = dr.commit_release_record(repo, "0.0.2")
    assert ok, note

    changed = run("show", "--name-only", "--format=", "HEAD").stdout.split()
    assert changed == ["VERSION"], (
        f"the record commit swept in files this release did not change: {changed}")
