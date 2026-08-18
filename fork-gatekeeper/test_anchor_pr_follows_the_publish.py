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


def test_anchor_worktree_has_an_empty_tree_for_diff_scoped_benchmark_gates(tmp_path):
    """The corpus is untracked, so it is absent from a throwaway worktree.

    The gate is still allowed to prove that this branch changed no benchmark
    paths.  A real empty directory provides that context without borrowing the
    primary checkout's unrelated HEAD through a symlink.
    """
    ok, note = P._prepare_anchor_gate_context(tmp_path)
    assert ok is True, note
    tree = tmp_path / P.BENCHMARK_TREE
    assert tree.is_dir()
    assert list(tree.iterdir()) == []
    assert not tree.is_symlink()


def test_anchor_gate_context_refuses_a_non_directory_collision(tmp_path):
    (tmp_path / P.BENCHMARK_TREE).write_text("not a corpus")
    ok, note = P._prepare_anchor_gate_context(tmp_path)
    assert ok is False
    assert "not a directory" in note


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


def test_an_anchor_exception_is_captured_as_a_terminal_round_result():
    """The image is irrevocably public, but the round is not converged.

    The exception must be retained as evidence rather than escaping before the
    current-round sidecar is written. A later noop retries the anchor.
    """
    src = Path(DR.__file__).read_text()
    seg = src.split("open_anchor_pr", 1)[1][:400]
    assert "except Exception" in src.split("open_anchor_pr", 1)[0][-600:] or \
           "except Exception" in seg, "the call is not wrapped"
    assert '"stage": "anchor_pr"' in Path(DR.__file__).read_text()


def test_noop_retries_anchor_and_main_already_current_is_success():
    release = Path(DR.__file__).read_text()
    anchor = Path(P.__file__).read_text()
    assert release.count("open_anchor_pr") >= 2
    assert "anchor already at {version} — nothing to open" in anchor
    assert "branch_exists" in anchor and "anchor PR pending" in anchor


def test_the_anchor_branch_is_pushed_by_full_refname():
    """A detached HEAD cannot push to a SHORT destination name.

    The anchor worktree is created with `--detach` (so no local branch-name
    residue is left behind).  From a detached HEAD git cannot infer whether
    `HEAD:eda-anchor-0.3.7` means a branch or a tag, so it refuses:

        error: The destination you provided is not a full refname (i.e.,
        starting with "refs/"). We tried to guess what you meant by:
        - Looking for a ref that matches 'eda-anchor-0.3.7' on the remote side.

    Reproduced standalone on git 2.x: from a detached HEAD,
    `push origin HEAD:eda-anchor-0.3.7` errors and
    `push origin HEAD:refs/heads/eda-anchor-0.3.7` returns 0.

    Measured live on the 0.3.7 release: the image built, smoked, recorded and
    published, and only the anchor PR failed -- so `:latest` moved off the
    version vibe-ic pins, which is precisely the split vibe-ic#754 closed and
    this file exists to keep closed.
    """
    src = Path(P.__file__).read_text()
    body = src.split("def open_anchor_pr", 1)[1].split("\ndef ", 1)[0]
    assert '"push"' in body, "open_anchor_pr no longer pushes the anchor branch"
    # Match the refspec itself rather than a source LINE: the call is wrapped,
    # so a line-wise check passes or fails on formatting instead of on the
    # thing under test.
    assert 'f"HEAD:refs/heads/{branch}"' in body, (
        "the anchor branch is not pushed by full refname; from the detached "
        "anchor worktree git refuses a short destination")
    assert 'f"HEAD:{branch}"' not in body, (
        "a short-name push destination survives in open_anchor_pr")
