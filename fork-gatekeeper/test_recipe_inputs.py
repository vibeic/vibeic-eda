"""Declared build helpers must change the existing artefact identity.

These tests exercise the release producer and its existing blocking pin checker,
not merely a new digest helper. The first two controls execute on pre-fix code.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fork-gatekeeper"))
sys.path.insert(0, str(ROOT / "tools"))
import daily_release as release  # noqa: E402
import check_pins_agree as pins  # noqa: E402

REFS = {"WIDGET_REF": "a" * 40}
TARGETS = {"widget": ["WIDGET_REF"]}


def _tree(tmp_path):
    root = tmp_path / "eda"
    tool = root / "tools" / "widget"
    tool.mkdir(parents=True)
    (tool / "Dockerfile").write_text(
        "# recipe-inputs: helper.py\nFROM scratch\n"
        f"ARG WIDGET_REF={REFS['WIDGET_REF']}\n"
        "COPY helper.py /helper.py\n")
    (tool / "helper.py").write_bytes(b"print('first')\n")
    (root / "docker-bake.hcl").write_text(
        f'variable "WIDGET_REF" {{ default = "{REFS["WIDGET_REF"]}" }}\n'
        'variable "WIDGET_RECIPE" { default = "unpublished" }\n'
        'target "widget" { context = "tools/widget" }\n')
    (root / "Dockerfile").write_text(
        "ARG IMG_WIDGET=ghcr.io/vibeic/eda-tool-widget:unpublished\n")
    _publish(root)
    return root, tool


def _publish(root):
    release.write_recipe_vars(root, TARGETS)
    release.retag_images(root, REFS, TARGETS)


def _tag(root):
    return release.artefact_tag(root, "widget", TARGETS["widget"], REFS)


def _checker(root, monkeypatch):
    monkeypatch.setattr(pins, "ROOT", root)
    monkeypatch.setattr(pins, "TOOLS", TARGETS)
    return pins.main()


def test_helper_only_change_moves_actual_release_artefact_tag(tmp_path):
    root, tool = _tree(tmp_path)
    before = _tag(root)
    (tool / "helper.py").write_bytes(b"print('second')\n")
    after = _tag(root)
    assert before != after, "helper bytes changed but the release reused its tag"
    assert before.rsplit("-", 1)[0] == after.rsplit("-", 1)[0]


def test_existing_pin_checker_blocks_stale_helper_digest(tmp_path, monkeypatch):
    root, tool = _tree(tmp_path)
    assert _checker(root, monkeypatch) == 0
    (tool / "helper.py").write_bytes(b"print('second')\n")
    assert _checker(root, monkeypatch) == 1


def test_republishing_helper_digest_restores_three_site_agreement(tmp_path, monkeypatch):
    root, tool = _tree(tmp_path)
    (tool / "helper.py").write_bytes(b"print('second')\n")
    _publish(root)
    assert _checker(root, monkeypatch) == 0
    assert release.write_recipe_vars(root, TARGETS) == []


def test_real_legacy_recipe_corpus_keeps_exact_hash():
    """Read every real non-opted-in tool, not a hand-copied recipe fixture."""
    measured = []
    for dockerfile in sorted((ROOT / "tools").glob("*/Dockerfile")):
        data = dockerfile.read_bytes()
        if b"recipe-inputs:" in data:
            continue
        assert release.recipe_hash(ROOT, dockerfile.parent.name) == (
            hashlib.sha256(data).hexdigest()[:6])
        measured.append(dockerfile.parent.name)
    assert measured, "no real legacy recipe was measured"


def test_real_declared_helpers_change_actual_release_tags(tmp_path):
    """Exercise the real opted-in recipe and each actual checked-in helper."""
    measured = []
    for dockerfile in sorted((ROOT / "tools").glob("*/Dockerfile")):
        lines = [line for line in dockerfile.read_text().splitlines()
                 if line.strip().startswith("# recipe-inputs:")]
        if not lines:
            continue
        tool = dockerfile.parent.name
        copied = tmp_path / "tools" / tool
        shutil.copytree(dockerfile.parent, copied)
        for name in lines[0].split(":", 1)[1].split():
            helper = copied / name
            original = helper.read_bytes()
            before = release.artefact_tag(tmp_path, tool, ["WIDGET_REF"], REFS)
            helper.write_bytes(original + b"\n# recipe identity mutation\n")
            assert release.artefact_tag(tmp_path, tool, ["WIDGET_REF"], REFS) != before
            helper.write_bytes(original)
            measured.append((tool, name))
    assert measured, "no real declared helper input was measured"


def test_declared_multiple_binary_inputs_are_relocatable_and_all_hashed(tmp_path):
    root, tool = _tree(tmp_path)
    dockerfile = tool / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace(
        "recipe-inputs: helper.py", "recipe-inputs: helper.py nested/second.bin"))
    (tool / "nested").mkdir()
    (tool / "nested" / "second.bin").write_bytes(b"\0\xfffirst")
    before = _tag(root)
    copied = tmp_path / "copied"
    shutil.copytree(root, copied)
    assert _tag(copied) == before
    (tool / "nested" / "second.bin").write_bytes(b"\0\xffsecond")
    assert _tag(root) != before
    (tool / "unrelated.log").write_bytes(b"not a declared build input")
    after = _tag(root)
    (tool / "unrelated.log").write_bytes(b"changed unrelated output")
    assert _tag(root) == after


@pytest.mark.parametrize("declaration", [
    "", "helper.py helper.py", "../outside.py", "/tmp/outside.py",
    "./helper.py", "nested/../helper.py", "nested//helper.py", "Dockerfile",
    "helper.py\n# recipe-inputs: helper.py", "helper.py\\other.py",
])
def test_invalid_declared_inputs_fail_closed(tmp_path, declaration):
    root, tool = _tree(tmp_path)
    (tool / "Dockerfile").write_text(
        f"# recipe-inputs: {declaration}\nFROM scratch\n")
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)


def test_missing_declared_input_blocks_producer_and_checker(tmp_path, monkeypatch, capsys):
    root, tool = _tree(tmp_path)
    (tool / "helper.py").unlink()
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)
    assert _checker(root, monkeypatch) == 1
    assert "recipe-inputs" in capsys.readouterr().err


def test_malformed_directive_is_not_a_legacy_fallback(tmp_path):
    root, tool = _tree(tmp_path)
    (tool / "Dockerfile").write_text("# recipe-inputs helper.py\nFROM scratch\n")
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)


def test_directory_is_not_a_readable_helper(tmp_path):
    root, tool = _tree(tmp_path)
    (tool / "helper.py").unlink()
    (tool / "helper.py").mkdir()
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)


def test_symlink_parent_directory_is_rejected(tmp_path):
    root, tool = _tree(tmp_path)
    (tool / "Dockerfile").write_text(
        "# recipe-inputs: nested/helper.py\nFROM scratch\n")
    (tool / "nested").symlink_to(tool, target_is_directory=True)
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)


@pytest.mark.parametrize("escape", [False, True])
def test_symlink_inputs_are_not_silently_followed(tmp_path, escape):
    root, tool = _tree(tmp_path)
    target = (tmp_path if escape else tool) / "target.py"
    target.write_bytes(b"content outside COPY identity")
    (tool / "helper.py").unlink()
    (tool / "helper.py").symlink_to(target)
    with pytest.raises(ValueError, match="recipe-inputs"):
        _tag(root)


def test_legacy_recipe_ignores_unrelated_files(tmp_path):
    root, tool = _tree(tmp_path)
    dockerfile = tool / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    before = _tag(root)
    (tool / "helper.py").write_bytes(b"not declared")
    assert _tag(root) == before
    assert release.recipe_hash(root, "widget") == (
        hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:6])
