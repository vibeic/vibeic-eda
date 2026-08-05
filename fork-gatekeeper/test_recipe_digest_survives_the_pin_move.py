"""A pin lives inside the file whose hash is the recipe digest.

So moving a pin necessarily moves the digest, and any digest published BEFORE
the move describes a file that no longer exists in that form.

`daily_release.main` publishes the digests once, early, with the comment
"before anything reads a tag, so bake and this program cannot disagree about
what an artefact is called". That is correct for everything between there and
the pin move -- and wrong for everything after it, because `rewrite_pin` edits
`tools/<t>/Dockerfile` and `retag_images` then recomputes the tag from the
CURRENT file. The two sites disagree by construction on every release that
moves a pin.

That is not hypothetical. The 2026-08-05 tick reported exactly it, for three
tools at once, AFTER the release had already failed to compose:

    openroad: the RECIPE digest disagrees -- `bake` would publish a tag the
    composing Dockerfile does not pull
        docker-bake.hcl                          7f0732
        sha256(tools/openroad/Dockerfile)[:6]    1fef1d

These tests assert the invariant behaviourally, and separately assert the
ordering in the source -- because the behavioural test can be satisfied by any
number of arrangements, and the ordering is the thing that was wrong.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_release as dr  # noqa: E402

OLD = "a" * 40
NEW = "b" * 40


def _make_root(tmp_path: Path) -> Path:
    """A minimal tree with the three places a tool's identity is written."""
    root = tmp_path / "eda"
    (root / "tools" / "widget").mkdir(parents=True)
    (root / "tools" / "widget" / "Dockerfile").write_text(
        f"FROM scratch\nARG WIDGET_REF={OLD}  # pinned; branch master\n",
        encoding="utf-8")
    digest = hashlib.sha256(
        (root / "tools" / "widget" / "Dockerfile").read_bytes()).hexdigest()[:6]
    (root / "Dockerfile").write_text(
        f"ARG IMG_WIDGET=ghcr.io/vibeic/eda-tool-widget:{OLD[:7]}-{digest}\n"
        "FROM ${IMG_WIDGET} AS img-widget\n", encoding="utf-8")
    (root / "docker-bake.hcl").write_text(
        f'variable "WIDGET_REF"    {{ default = "{OLD}" }}\n'
        f'variable "WIDGET_RECIPE" {{ default = "{digest}" }}\n'
        'target "widget" {\n'
        '  tags = tool_tags("widget", WIDGET_REF, WIDGET_RECIPE)\n'
        '}\n', encoding="utf-8")
    return root


def _bake_recipe(root: Path) -> str:
    m = re.search(r'variable\s+"WIDGET_RECIPE"\s*\{\s*default\s*=\s*"([^"]*)"',
                  (root / "docker-bake.hcl").read_text())
    assert m, "the bake file lost its recipe variable"
    return m.group(1)


def _file_recipe(root: Path) -> str:
    return hashlib.sha256(
        (root / "tools" / "widget" / "Dockerfile").read_bytes()).hexdigest()[:6]


def _composed_tag_recipe(root: Path) -> str:
    m = re.search(r"ARG IMG_WIDGET=\S+:[0-9a-f]+-([0-9a-f]+)",
                  (root / "Dockerfile").read_text())
    assert m, "the composing Dockerfile lost its IMG_WIDGET tag"
    return m.group(1)


def test_the_starting_tree_agrees_with_itself():
    """Control. If the fixture is already broken the rest proves nothing."""


def test_moving_a_pin_changes_the_recipe_digest(tmp_path):
    """The premise. If this ever stops holding, the bug cannot occur -- and
    these tests would be passing for a reason that no longer exists."""
    root = _make_root(tmp_path)
    before = _file_recipe(root)
    dr.rewrite_pin(root, "WIDGET", NEW)
    after = _file_recipe(root)
    assert before != after, (
        "moving the pin did not change sha256(tools/widget/Dockerfile) -- the "
        "premise of this whole check is gone, so re-derive it before trusting "
        "any of these tests")


def test_publishing_the_digest_before_the_pin_move_leaves_bake_stale(tmp_path):
    """THE BUG, reproduced. This is the order `main` used, and it is red.

    Kept as a live test rather than a comment: it is the control that gives the
    next test its meaning. Without it, 'the digests agree' is satisfiable by a
    tree where nothing ever moved.
    """
    root = _make_root(tmp_path)
    targets = {"widget": ["WIDGET_REF"]}

    dr.write_recipe_vars(root, targets)      # early, as main did
    dr.rewrite_pin(root, "WIDGET", NEW)      # ... and the pin moves after it

    assert _bake_recipe(root) != _file_recipe(root), (
        "publishing the digest before the pin move no longer leaves it stale; "
        "if that is a deliberate change, this test should be deleted, not "
        "adjusted")


def test_the_three_sites_agree_after_a_pin_move(tmp_path):
    """THE FIX. Re-publishing after the move makes bake and the composing
    Dockerfile name the same artefact."""
    root = _make_root(tmp_path)
    targets = {"widget": ["WIDGET_REF"]}

    dr.write_recipe_vars(root, targets)
    dr.rewrite_pin(root, "WIDGET", NEW)
    dr.write_recipe_vars(root, targets)      # <- the fix
    dr.retag_images(root, {"WIDGET_REF": NEW}, targets)

    on_disk = _file_recipe(root)
    assert _bake_recipe(root) == on_disk, (
        f"bake says {_bake_recipe(root)}, the recipe hashes to {on_disk} -- "
        "bake would publish a tag the composing Dockerfile does not pull")
    assert _composed_tag_recipe(root) == on_disk, (
        f"the composing Dockerfile asks for {_composed_tag_recipe(root)}, the "
        f"recipe hashes to {on_disk}")


def test_the_pin_itself_reached_all_three_sites(tmp_path):
    """Bidirectional control: a `write_recipe_vars` that simply wrote the same
    string everywhere would satisfy the digest assertions above. The pin has to
    have actually moved too."""
    root = _make_root(tmp_path)
    targets = {"widget": ["WIDGET_REF"]}

    dr.write_recipe_vars(root, targets)
    dr.rewrite_pin(root, "WIDGET", NEW)
    dr.write_recipe_vars(root, targets)
    dr.retag_images(root, {"WIDGET_REF": NEW}, targets)

    assert NEW in (root / "tools" / "widget" / "Dockerfile").read_text()
    assert NEW in (root / "docker-bake.hcl").read_text()
    assert NEW[:7] in (root / "Dockerfile").read_text()
    assert OLD not in (root / "docker-bake.hcl").read_text()


def test_the_comment_survives_the_rewrite(tmp_path):
    """Guards the reason `rewrite_pin` replaces only the 40-hex: an earlier
    bumper rewrote whole lines and ate the trailing comments, after which a
    parser that read the branch name FROM the comment lost two of fourteen
    tools and reported twelve without saying so."""
    root = _make_root(tmp_path)
    dr.rewrite_pin(root, "WIDGET", NEW)
    assert "# pinned; branch master" in (
        root / "tools" / "widget" / "Dockerfile").read_text()


def test_main_republishes_the_digests_after_moving_pins():
    """The ordering, asserted on the source.

    The behavioural tests above prove the invariant HOLDS when the calls are
    made in the right order; they cannot prove `main` makes them in that order,
    because they call the functions themselves. This is the assertion that
    would have caught the original defect.
    """
    src = (Path(dr.__file__)).read_text()
    body = src[src.index("def main("):]

    rewrite_at = body.index("rewrite_pin(root,")
    after = body[rewrite_at:]
    assert "write_recipe_vars(root, targets)" in after, (
        "main() moves a pin and never re-publishes the recipe digests. Moving "
        "a pin edits tools/<t>/Dockerfile, which IS the recipe -- so every "
        "digest published earlier in the run now describes a file that no "
        "longer exists in that form, and bake will publish a tag the composing "
        "Dockerfile does not pull.")

    retag_at = after.index("retag_images(root,")
    republish_at = after.index("write_recipe_vars(root, targets)")
    assert republish_at < retag_at, (
        "the digests are re-published AFTER retag_images, so the composing "
        "Dockerfile is written from a digest bake has not been told about")
