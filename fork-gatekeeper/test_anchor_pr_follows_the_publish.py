"""A release publishes an image. It does not check anything into vibe-ic.

WHAT THIS FILE USED TO GUARD, AND WHY THAT RULE IS GONE.

vibe-ic#754 tied publishing and anchoring into one action: publish the image,
then open a PR moving vibe-ic's pinned version, because otherwise `:latest`
drifted off the version vibe-ic pinned and the two were reunited only when a
landing gate happened to look. Tests here pinned that tie from every angle.

The tie rested on a sentence in vibe-ic's own source: the pinned tag "matches
what the plugin was VERIFIED AGAINST". Measured 2026-08-20, nothing in the
release ever verified that. The round established that the tag was PULLABLE and
then wrote a claim about verification that had not happened — on every release.

And the claim was not needed. This image is built FOR vibe-ic and sits under it,
and its own release gate already establishes what the upper layer would have
re-checked: `check_no_capability_lost` proves 78 commands across 17 replaced
prefixes still resolve in the new image, 439 fork self-checks run, every declared
PDK is present, and the build refuses unless sby/yices, ALIGN, klayout and the
xyce plugin builder all work. A release that passes that is usable by
construction; asking the layer above to confirm it is asking for a second opinion
from somewhere with less evidence.

So the anchor is not moved from here any more — not by PR, not by push. The three
tests left are the ones that were never about the anchor: they guard
`_prepare_push_gate_context`, which every remaining vibe-ic PR path still needs.

The cost this removes, for the record: an anchor PR sat open on vibe-ic, which
made `fork_github_convergence` non-zero, which skipped the NEXT round's certified
publish. Round 5 on 2026-08-20 merged all 37 forks clean, passed every gate, and
was blocked by the three PRs it had itself just opened.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_notify as P                                          # noqa: E402
import daily_release as DR                                     # noqa: E402






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












# ---------------------------------------------------------------------------
# The gap that produced this test: the benchmark-tree context was written for
# `open_anchor_pr` and never called from `open_pr` or `open_assessment_pr`, so
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
    # NOT a count. It was `>= 3` while three PR paths lived here; the alarm has
    # since moved its worktree into `check_publish_silence` (it files on THIS
    # repo, not vibe-ic), so pinning the number turned a correct removal into a
    # red suite that blocked a round. The property is "every worktree-creating
    # function here prepares the context", and that holds at any count above
    # zero — the `assert pushing` above already refuses zero, which is the case
    # that would mean the scan itself broke.
    assert len(pushing) >= 1



# ── the new rule, guarded ───────────────────────────────────────────────────

def test_the_release_checks_nothing_into_vibe_ic():
    """A release publishes an image and stops.

    Deleting the old guards without adding this one would leave the rule
    unstated — and a rule nothing asserts is a rule that comes back. Anyone
    re-wiring an anchor into the release path turns this red."""
    release = Path(DR.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in release.splitlines()
                     if not l.lstrip().startswith("#"))
    for gone in ("open_anchor_pr", "push_anchor", "anchor_pr_not_established"):
        assert gone not in code, (
            f"daily_release calls {gone!r} again. Publishing an image is not "
            "verifying a plugin against it, and the layer below already proves "
            "the image is usable — see this module's docstring.")


def test_the_anchor_capability_is_gone_from_pr_notify():
    """The mechanism, not just its call site. A dead function that still works
    is an invitation to re-wire it."""
    import ast
    src = Path(P.__file__).read_text(encoding="utf-8")
    names = {n.name for n in ast.parse(src).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "open_anchor_pr" not in names
    assert "push_anchor" not in names
    # `open_alert_pr` is gone too: the publish-silence alarm files on THIS repo
    # now, so its worktree and PR live in `check_publish_silence` rather than in
    # the vibe-ic notifier. What must not vanish silently are the two paths this
    # file's other tests guard.
    assert {"open_pr", "open_assessment_pr"} <= names
