#!/usr/bin/env python3
"""Tests for the pin -> artefact -> image release chain.

WHY THESE EXIST
===============
`daily_release.py` shipped five defects in one day, each of which made a broken
state report as a clean one:

  1. pins resolved through a branch RANKING with no containment check, proposing
     to move cadical's pin onto an upstream `sweep` branch;
  2. rebuild triggered on "the pin moved during this run" instead of "the
     artefact is absent", so an interrupted run never rebuilt;
  3. the same hole one level up — the composed IMAGE was not part of the
     absence test;
  4. `VERSION` was bumped from a file that had drifted (0.2.30 on disk, 0.2.32
     built), so the next number collided with an existing image;
  5. the compose tagged one image and the smoke read another, so the release
     would have published older bytes under the new number.

Four of the five were found by reading output or source, not by any test. Each
test below drives a real function against a real fixture tree and asserts the
observable result — never that the source contains a string, which is how a
gate stays green through a runtime failure on the line it claims to check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_release as R                                     # noqa: E402
import fork_reaches_flow_check as F                           # noqa: E402


def _tree(tmp_path: Path) -> Path:
    """A miniature eda-root: two single-repo tools and one two-repo tool."""
    root = tmp_path / "eda"
    (root / "tools" / "yosys").mkdir(parents=True)
    (root / "tools" / "lvs").mkdir(parents=True)
    (root / "tools" / "yosys" / "Dockerfile").write_text(
        "ARG YOSYS_REPO=https://github.com/vibeic/yosys.git\n"
        "ARG YOSYS_REF=" + "a" * 40 + "  # branch vibeic/integration\n"
        'RUN git clone "${YOSYS_REPO}" /y\n'
        "# github.com/vibeic/yosys \n")
    # one Dockerfile, TWO repos — a first-match parse silently drops one
    (root / "tools" / "lvs" / "Dockerfile").write_text(
        "ARG MAGIC_REF=" + "b" * 40 + "\n"
        "ARG NETGEN_REF=" + "c" * 40 + "\n"
        'RUN git clone https://github.com/vibeic/magic.git /m \\\n'
        ' && git clone https://github.com/vibeic/netgen.git /n\n')
    (root / "Dockerfile").write_text(
        "ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:aaaaaaa\n"
        "ARG IMG_LVS=ghcr.io/vibeic/eda-tool-lvs:bbbbbbb-ccccccc\n"
        "FROM ${IMG_YOSYS} AS img-yosys\n"
        "FROM ${IMG_LVS} AS img-lvs\n"
        "COPY --from=img-yosys /foss/tools/yosys /foss/tools/yosys\n"
        "COPY --from=img-lvs /foss/tools/magic /foss/tools/magic\n")
    (root / "docker-bake.hcl").write_text(
        'variable "YOSYS_REF"  { default = "' + "a" * 40 + '" }\n'
        'variable "MAGIC_REF"  { default = "' + "b" * 40 + '" }\n'
        'variable "NETGEN_REF" { default = "' + "c" * 40 + '" }\n'
        'target "yosys" {\n'
        '  context = "tools/yosys"\n'
        '  tags    = tool_tags("yosys", YOSYS_REF)\n'
        '}\n'
        'target "lvs" {\n'
        '  context = "tools/lvs"\n'
        '  tags    = ["${REGISTRY}/eda-tool-lvs:${short(MAGIC_REF)}-'
        '${short(NETGEN_REF)}"]\n'
        '}\n')
    (root / "VERSION").write_text("0.2.30\n")
    return root


def test_two_repos_in_one_dockerfile_are_both_found(tmp_path):
    """`tools/lvs` pins magic AND netgen; a first-match parse loses one."""
    root = _tree(tmp_path)
    args = R.ref_arg_names(root)
    assert args["magic"] == "MAGIC"
    assert args["netgen"] == "NETGEN", "netgen dropped — the second repo in a file"
    assert args["yosys"] == "YOSYS"


def test_tag_composition_is_read_from_bake_not_assumed(tmp_path):
    """`lvs` is tagged <magic>-<netgen>; bumping magic alone must retag."""
    root = _tree(tmp_path)
    targets = R.bake_targets(root)
    assert targets["lvs"] == ["MAGIC_REF", "NETGEN_REF"], "order matters in the tag"
    assert targets["yosys"] == ["YOSYS_REF"]


def test_rewrite_pin_moves_every_site_and_keeps_the_comment(tmp_path):
    """An earlier bumper rewrote whole lines with sed and ate the comments,
    after which a parser reading the branch FROM the comment lost two tools."""
    root = _tree(tmp_path)
    changed = R.rewrite_pin(root, "YOSYS", "d" * 40)
    assert "tools/yosys/Dockerfile" in changed
    assert "docker-bake.hcl" in changed
    df = (root / "tools" / "yosys" / "Dockerfile").read_text()
    assert "d" * 40 in df
    assert "# branch vibeic/integration" in df, "trailing comment destroyed"
    assert "d" * 40 in (root / "docker-bake.hcl").read_text()


def test_retag_recomposes_a_two_commit_tag(tmp_path):
    """Bumping magic while leaving `eda-tool-lvs:<magic>-<netgen>` untouched
    would keep pulling the artefact built before the bump."""
    root = _tree(tmp_path)
    refs = {"YOSYS_REF": "a" * 40, "MAGIC_REF": "e" * 40, "NETGEN_REF": "c" * 40}
    touched = R.retag_images(root, refs, R.bake_targets(root))
    assert any("IMG_LVS" in t for t in touched)
    assert "eda-tool-lvs:eeeeeee-ccccccc" in (root / "Dockerfile").read_text()


def test_version_rises_above_reality_not_just_the_file(tmp_path, monkeypatch):
    """VERSION read 0.2.30 while 0.2.32 was built; a file-only bump collides."""
    root = _tree(tmp_path)
    monkeypatch.setattr(R, "_existing_versions", lambda _r: [(0, 2, 31), (0, 2, 32)])
    old, new = R.peek_version(root)
    assert (old, new) == ("0.2.30", "0.2.33")
    assert (root / "VERSION").read_text().strip() == "0.2.30", \
        "peek must NOT write — the file moves only after a successful push"


def test_version_rolls_the_minor_at_99(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(R, "_existing_versions", lambda _r: [(0, 2, 99)])
    assert R.peek_version(root)[1] == "0.3.0"


def test_fingerprint_is_stable_and_moves_with_any_pin(tmp_path):
    """RELEASED.json's whole purpose: a pin set that differs has not shipped."""
    a = {"yosys": "a" * 40, "magic": "b" * 40}
    assert R.pins_fingerprint(a) == R.pins_fingerprint(dict(reversed(list(a.items()))))
    assert R.pins_fingerprint(a) != R.pins_fingerprint({**a, "magic": "c" * 40})
    assert R.pins_fingerprint(a) != R.pins_fingerprint({**a, "extra": "d" * 40})


def test_unreleased_when_no_record_and_released_when_it_matches(tmp_path):
    root = _tree(tmp_path)
    pins = {"yosys": "a" * 40}
    assert R.released_record(root) == {}, "no record means never released"
    (root / "RELEASED.json").write_text(json.dumps(
        {"version": "0.2.33", "pins_fingerprint": R.pins_fingerprint(pins)}))
    assert R.released_record(root)["pins_fingerprint"] == R.pins_fingerprint(pins)
    # the failure this guards: one pin moves, the record must no longer match
    assert R.released_record(root)["pins_fingerprint"] != \
        R.pins_fingerprint({"yosys": "z" * 40})


def test_copied_paths_reads_what_came_from_our_artefacts(tmp_path):
    """The containment test in fork_reaches_flow_check depends on this map."""
    root = _tree(tmp_path)
    ours = F.copied_paths(root / "Dockerfile")
    assert ours["yosys"] == ["/foss/tools/yosys"]
    assert ours["lvs"] == ["/foss/tools/magic"]


def test_provenance_drift_reports_not_checked_rather_than_a_finding(monkeypatch):
    """An image with no provenance compared nothing; that is not 1 drift row."""
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, "", ""))
    out = F.provenance_drift("img", {"yosys": "a" * 40})
    assert len(out) == 1 and out[0].get("not_checked") is True


def test_provenance_drift_finds_a_ref_the_pin_no_longer_names(monkeypatch):
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, json.dumps(
        {"tool": "yosys", "repo": "https://github.com/vibeic/yosys.git",
         "ref": "z" * 40}), ""))
    out = F.provenance_drift("img", {"yosys": "a" * 40})
    assert len(out) == 1 and not out[0].get("not_checked")
    assert out[0]["built_from"] == "z" * 9


def test_provenance_drift_is_silent_when_the_image_matches(monkeypatch):
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, json.dumps(
        {"tool": "yosys", "repo": "https://github.com/vibeic/yosys.git",
         "ref": "a" * 40}), ""))
    assert F.provenance_drift("img", {"yosys": "a" * 40}) == []


def test_artefact_tag_carries_every_ref_and_the_recipe(tmp_path):
    root = _tree(tmp_path)
    refs = {"MAGIC_REF": "b" * 40, "NETGEN_REF": "c" * 40}
    tag = R.artefact_tag(root, "lvs", ["MAGIC_REF", "NETGEN_REF"], refs)
    assert tag.startswith("ghcr.io/vibeic/eda-tool-lvs:bbbbbbb-ccccccc-")
    assert len(tag.rsplit("-", 1)[1]) == 6, "recipe component missing"


def test_changing_the_recipe_changes_the_tag(tmp_path):
    """#21: a recipe-only fix produced no new tag, so it could never ship."""
    root = _tree(tmp_path)
    refs = {"YOSYS_REF": "a" * 40}
    before = R.artefact_tag(root, "yosys", ["YOSYS_REF"], refs)
    df = root / "tools" / "yosys" / "Dockerfile"
    df.write_text(df.read_text() + "\n# install to a different prefix\n")
    after = R.artefact_tag(root, "yosys", ["YOSYS_REF"], refs)
    assert before != after, "the recipe changed and the artefact identity did not"
    assert before.rsplit("-", 1)[0] == after.rsplit("-", 1)[0], \
        "only the recipe component may move when only the recipe moved"


def test_artefact_tag_is_none_when_a_ref_is_unknown(tmp_path):
    """A tag built from a partial ref set would name the wrong artefact."""
    root = _tree(tmp_path)
    assert R.artefact_tag(root, "lvs", ["MAGIC_REF", "NETGEN_REF"],
                          {"MAGIC_REF": "b" * 40}) is None


def test_recipe_hash_says_so_when_there_is_no_dockerfile(tmp_path):
    root = _tree(tmp_path)
    assert R.recipe_hash(root, "does-not-exist") == "nofile"


def test_bake_and_the_program_agree_on_every_recipe(tmp_path):
    """#21's core: two expressions compose a tool tag and must not diverge."""
    root = _tree(tmp_path)
    hcl = root / "docker-bake.hcl"
    hcl.write_text(hcl.read_text() +
                   '\nvariable "YOSYS_RECIPE" { default = "stale0" }\n')
    moved = R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]})
    assert moved and moved[0].startswith("YOSYS_RECIPE=")
    assert R.bake_recipe_vars(root)["YOSYS"] == R.recipe_hash(root, "yosys")


def test_write_recipe_vars_is_idempotent(tmp_path):
    root = _tree(tmp_path)
    hcl = root / "docker-bake.hcl"
    hcl.write_text(hcl.read_text() +
                   '\nvariable "YOSYS_RECIPE" { default = "stale0" }\n')
    R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]})
    assert R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]}) == [], \
        "a second pass must report nothing moved"


def test_a_recipe_only_change_makes_the_release_fingerprint_move(tmp_path):
    """Over pins alone, two images with different bytes share a fingerprint."""
    root = _tree(tmp_path)
    pins = {"yosys": "a" * 40}
    before = R.pins_fingerprint({**pins,
                                 "recipe:yosys": R.recipe_hash(root, "yosys")})
    df = root / "tools" / "yosys" / "Dockerfile"
    df.write_text(df.read_text() + "\n# a different prefix\n")
    after = R.pins_fingerprint({**pins,
                                "recipe:yosys": R.recipe_hash(root, "yosys")})
    assert before != after
