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
    ok, note = P.push_anchor(bad)
    assert ok is False and "malformed" in note, note


def test_it_delegates_to_vibe_ics_own_anchor_tool():
    """NOT a regex over a private file list.

    `open_pr` bumps `DOC_FILES` — 2 files — while the anchor spans 15 ghcr
    pointers across 9. That gap is why a tick which DID fire still left the
    anchor failing its own gate. Delegating also inherits the tool's refusal to
    point at a tag `docker pull` cannot resolve, which a regex would happily
    write."""
    src = Path(P.__file__).read_text()
    body = src.split("def push_anchor", 1)[1].split("\ndef ", 1)[0]
    assert "ANCHOR_TOOL" in body, "the anchor PR must run vibe-ic's own tool"
    assert "--set" in body
    assert "_PIN_RE" not in body, (
        "push_anchor re-derived where the version is written; that second "
        "list is exactly what drifts from the first")


def test_anchor_worktree_has_an_empty_tree_for_diff_scoped_benchmark_gates(tmp_path):
    """The corpus is untracked, so it is absent from a throwaway worktree.

    The gate is still allowed to prove that this branch changed no benchmark
    paths.  A real empty directory provides that context without borrowing the
    primary checkout's unrelated HEAD through a symlink.
    """
    ok, note = P._prepare_push_gate_context(tmp_path)
    assert ok is True, note
    tree = tmp_path / P.BENCHMARK_TREE
    assert tree.is_dir()
    assert list(tree.iterdir()) == []
    assert not tree.is_symlink()


def test_anchor_gate_context_refuses_a_non_directory_collision(tmp_path):
    (tmp_path / P.BENCHMARK_TREE).write_text("not a corpus")
    ok, note = P._prepare_push_gate_context(tmp_path)
    assert ok is False
    assert "not a directory" in note


def test_the_release_opens_the_anchor_pr_only_on_a_real_publish():
    """A LOCAL ONLY build has nothing to anchor TO. Pointing the repo at a tag
    nobody can pull is worse than leaving it behind one that resolves."""
    src = Path(DR.__file__).read_text()
    assert "push_anchor" in src, (
        "the release path does not call it — the capability existing and the "
        "release not using it is the defect #754 describes")
    seg = src.split("push_anchor", 1)[0]
    tail = seg[-900:]
    assert re.search(r"if pushed:", tail), (
        "the anchor call is not guarded by `pushed`")


def test_an_anchor_exception_is_captured_as_a_terminal_round_result():
    """The image is irrevocably public, but the round is not converged.

    The exception must be retained as evidence rather than escaping before the
    current-round sidecar is written. A later noop retries the anchor.
    """
    src = Path(DR.__file__).read_text()
    seg = src.split("push_anchor", 1)[1][:400]
    assert "except Exception" in src.split("push_anchor", 1)[0][-600:] or \
           "except Exception" in seg, "the call is not wrapped"
    assert '"stage": "anchor_pr"' in Path(DR.__file__).read_text()


def test_noop_retries_anchor_and_main_already_current_is_success():
    release = Path(DR.__file__).read_text()
    anchor = Path(P.__file__).read_text()
    assert release.count("push_anchor") >= 2
    assert "anchor already at {version} — nothing to push" in anchor
    # `branch_exists` / "anchor PR pending" were the PR-era guard against
    # opening a second PR for a version whose branch was still on origin. A
    # direct push has no pending branch: it lands or it is reported. The
    # properties that carry over are asserted instead — "already at this
    # version" is SUCCESS (above), and a contended push is retried ONCE and
    # then reported rather than looped.
    assert "non-fast-forward" in anchor
    assert "for attempt in (1, 2)" in anchor


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
    body = src.split("def push_anchor", 1)[1].split("\ndef ", 1)[0]
    assert '"push"' in body, "push_anchor no longer pushes the anchor"
    # Match the refspec itself rather than a source LINE: the call is wrapped,
    # so a line-wise check passes or fails on formatting instead of on the
    # thing under test.
    assert '"HEAD:refs/heads/main"' in body, (
        "the anchor branch is not pushed by full refname; from the detached "
        "anchor worktree git refuses a short destination")
    assert 'f"HEAD:{branch}"' not in body, (
        "a short-name push destination survives in push_anchor")


def test_the_anchor_is_told_the_published_tag_not_left_to_discover_it():
    """We verified the push; the anchor must not re-derive it from a cache.

    `daily_release` reaches the anchor step only after
    `verify_remote_immutable_publication` has read the tag back with
    `docker manifest inspect` -- authoritative, immediate, and the same thing
    `docker pull` consults. Absent an override, vibe-ic's anchor tool re-asks
    the question through GHCR's `/tags/list`, which is CACHED.

    Measured 2026-08-19, unchanged tool, one hour after 0.3.12 was pushed and
    while its manifest resolved fine:

        no override  -> UNRESOLVABLE ADOPTION TARGET: 0.3.12 does not exist
                        on the registry (newest published: 0.3.11)
        override set -> adoption-target-resolves : OK (source=override)

    Without the override the release recorded itself anchor-blocked and the
    public page told readers no version had shipped and the lag below was real
    -- three false statements about an image that was in the registry the whole
    time. Whether OUR image reached the registry is this repo's question,
    already answered by this repo's readback.
    """
    src = Path(P.__file__).read_text()
    body = src.split("def push_anchor", 1)[1].split("\ndef ", 1)[0]
    call = [l for l in body.splitlines() if "ANCHOR_TOOL" in l or '"--set"' in l]
    assert call, "push_anchor no longer invokes the anchor tool"
    # CODE ONLY. The rationale above the call names the variable too, so a
    # substring check over the whole body passes on the comment alone -- it
    # stayed green with the override deleted.
    code = "\n".join(l for l in body.splitlines()
                     if not l.lstrip().startswith("#"))
    assert 'env={"VIBEIC_EDA_PUBLISHED_TAG": version}' in code, (
        "the anchor tool is invoked without VIBEIC_EDA_PUBLISHED_TAG, so it "
        "re-discovers the published tag from GHCR's cached /tags/list and can "
        "refuse a release we have already verified")


# ---------------------------------------------------------------------------
# The gap that produced this test: the benchmark-tree context was written for
# `push_anchor` and never called from `open_pr` or `open_assessment_pr`, so
# on 2026-08-20 `[notify:assess]` and `[notify:sync]` both failed the vibe-ic
# push gate with "UNDETERMINED: --tree benchmark-data is not a directory", the
# gatekeeper tick exited 4, and no PR went out. Naming the helper after one
# caller is part of how that survived; asserting the property is the rest.
# ---------------------------------------------------------------------------

def test_every_pr_path_prepares_the_push_gate_context():
    """Any function that creates a throwaway worktree must prepare the context.

    A structural assertion, not a list of three names: a FOURTH pushing path
    added later is caught by the same test, which is the failure mode that
    actually happened."""
    import ast

    src = Path(P.__file__).read_text()
    tree = ast.parse(src)

    def _calls(node):
        return {n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    def _makes_worktree(node):
        for n in ast.walk(node):
            if not isinstance(n, ast.List):
                continue
            parts = [e.value for e in n.elts if isinstance(e, ast.Constant)]
            if "worktree" in parts and "add" in parts:
                return True
        return False

    pushing = [f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and _makes_worktree(f)]
    assert pushing, "no worktree-creating function found — the scan is broken"

    missing = [f.name for f in pushing
               if "_prepare_push_gate_context" not in _calls(f)]
    assert not missing, (
        "these functions create a worktree they will push from but never "
        f"prepare the vibe-ic push gate's benchmark tree: {missing}. "
        "Without it the gate reports UNDETERMINED and the push is refused.")
    assert len(pushing) >= 3, f"expected the three known PR paths, saw {pushing}"
