#!/usr/bin/env python3
"""The published image must report OUR version, not the base image's.

`org.opencontainers.image.version` and `.ref.name` were never set in this
repo's bake target, so they came through from the iic-osic-tools base: the
version of what we BUILD ON, presented as the version of what we SHIP.
Measured 2026-08-21 on the published 0.3.17 —

    org.opencontainers.image.version = 2026.06
    org.opencontainers.image.ref.name = 2026.06
    IIC_OSIC_TOOLS_VERSION = 2026.06

— and nothing anywhere carried 0.3.17. Anything asking the standard question
"which version is this image" got a wrong answer with no way to tell.

It survived because nothing ever asked. That is what this file changes.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BAKE = _ROOT / "docker-bake.hcl"
_RELEASE = pathlib.Path(__file__).resolve().parent / "daily_release.py"
_BASE_VERSION = "2026.06"


def _printed(*extra: str) -> dict:
    """`bake --print` for the eda target, as the release will resolve it."""
    out = subprocess.run(
        ["docker", "buildx", "bake", "-f", str(_BAKE), "--print", *extra, "eda"],
        capture_output=True, text=True, cwd=str(_ROOT))
    body = out.stdout[out.stdout.index("{"):] if "{" in out.stdout else ""
    if not body:
        pytest.skip(f"bake --print unavailable: {(out.stderr or '')[:120]}")
    return json.loads(body)["target"]["eda"]


def test_the_bake_target_sets_our_version_labels():
    labels = _printed().get("labels") or {}
    assert "org.opencontainers.image.version" in labels
    assert "org.opencontainers.image.ref.name" in labels


def test_the_release_tag_becomes_the_version_label(monkeypatch):
    """The whole point: publishing 0.3.18 must make the image SAY 0.3.18."""
    monkeypatch.setenv("TAG", "0.3.18")
    target = _printed()
    labels = target["labels"]
    assert labels["org.opencontainers.image.version"] == "0.3.18"
    assert labels["org.opencontainers.image.ref.name"] == "0.3.18"
    assert any(t.endswith(":0.3.18") for t in target["tags"]), target["tags"]


def test_the_version_label_is_never_the_base_images(monkeypatch):
    """THE DEFECT, stated directly. 2026.06 is the base's version and belongs
    only under the key that says so."""
    monkeypatch.setenv("TAG", "0.3.18")
    labels = _printed()["labels"]
    assert labels["org.opencontainers.image.version"] != _BASE_VERSION
    assert labels.get("io.vibeic.base.version") == _BASE_VERSION, (
        "the base version is not deleted — it moves to a key where it is true")


def test_the_release_passes_the_version_through_the_environment():
    """`--set eda.labels.org.opencontainers.image.ref.name=` does NOT work:
    `--set` treats dots as path separators, so it creates a new `ref-name`
    label and leaves the real one at the default. Measured. The release must
    therefore set the TAG variable, which resolves tags and both labels from
    one value."""
    src = _RELEASE.read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert 'extra_env={"TAG"' in code, (
        "the release must give bake a TAG, or every published image is "
        "labelled with the bake default")
    assert "eda.labels." not in code, (
        "--set on a dotted label key silently creates a different label")


def test_a_default_build_is_not_silently_labelled_dev():
    """Without a TAG the labels say `dev`, which is honest for a local build
    and must never reach a release — hence the test above."""
    labels = _printed().get("labels") or {}
    assert labels["org.opencontainers.image.version"] == "dev"
