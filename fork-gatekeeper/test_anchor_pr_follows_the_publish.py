"""The eda image publish and the vibe-ic anchor bump are ONE action (vibe-ic#754).

They were two, with nothing linking them, so `:latest` moved off the version
vibe-ic pins on every release and the two were reunited only when a landing gate
happened to look. The repair was applied by hand four times before this.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_notify as P                                          # noqa: E402
import daily_release as DR                                     # noqa: E402


@pytest.mark.parametrize("bad", ["", "latest", "0.2", "0.2.63x", "v0.2.63", None])
def test_a_malformed_version_is_refused_before_any_git_runs(bad):
    """An anchor is a claim that a specific tag resolves. Nothing that is not a
    version may reach the tree, and the refusal must not depend on git."""
    ok, note = P.open_anchor_pr(bad)
    assert ok is False and "malformed" in note, note


def test_it_delegates_to_vibe_ics_own_anchor_tool():
    """NOT a regex over a private file list.

    `open_pr` bumps `DOC_FILES` — 2 files — while the anchor spans 15 ghcr
    pointers across 9. That gap is why a tick which DID fire still left the
    anchor failing its own gate. Delegating also inherits the tool's refusal to
    point at a tag `docker pull` cannot resolve, which a regex would happily
    write."""
    src = Path(P.__file__).read_text()
    body = src.split("def open_anchor_pr", 1)[1].split("\ndef ", 1)[0]
    assert "ANCHOR_TOOL" in body, "the anchor PR must run vibe-ic's own tool"
    assert "--set" in body
    assert "_PIN_RE" not in body, (
        "open_anchor_pr re-derived where the version is written; that second "
        "list is exactly what drifts from the first")


def test_the_release_opens_the_anchor_pr_only_on_a_real_publish():
    """A LOCAL ONLY build has nothing to anchor TO. Pointing the repo at a tag
    nobody can pull is worse than leaving it behind one that resolves."""
    src = Path(DR.__file__).read_text()
    assert "open_anchor_pr" in src, (
        "the release path does not call it — the capability existing and the "
        "release not using it is the defect #754 describes")
    seg = src.split("open_anchor_pr", 1)[0]
    tail = seg[-900:]
    assert re.search(r"if pushed:", tail), (
        "the anchor call is not guarded by `pushed`")


def test_a_failed_anchor_never_fails_the_release():
    """The image is already published by this point. An advisory step that can
    turn a successful release into a failed one would be traded away the first
    time it misfired."""
    src = Path(DR.__file__).read_text()
    seg = src.split("open_anchor_pr", 1)[1][:400]
    assert "except Exception" in src.split("open_anchor_pr", 1)[0][-600:] or \
           "except Exception" in seg, "the call is not wrapped"
