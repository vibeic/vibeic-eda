"""The certified page rendered "?" for the image tool count.

The refusal was right as far as it went: `manifest_identity` is a hash of
normalized manifest JSON, not an OCI digest, so it cannot qualify a Docker
reference — and inspecting an ambient `:latest` would attach some other image's
number to a certified release.

But that identity is exactly what turns a tag back into proof. Re-measure the
registry tag the way the release measured it and compare with what the release
RECORDED; equal means this tag is the certified image. The same answer carries
the OCI digest, so the count is taken from `image@sha256:…`.

MEASURED 2026-08-21 on 0.3.22: recomputed identity c86d53d6… equals the recorded
one, digest sha256:034828bb… equals the local RepoDigest, and the image ships 60
tools under /foss/tools.

The value is in the refusals, so that is what most of this file tests. A number
nobody proved is worse than "?", which is why every failure keeps "?".
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_page as PAGE  # noqa: E402

VERSION = "9.9.9"
DIGEST = "sha256:" + "0" * 64
MANIFEST = {"schemaVersion": 2, "layers": [{"digest": "sha256:" + "a" * 64}]}
IDENTITY = hashlib.sha256(
    json.dumps(MANIFEST, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")).hexdigest()


def _ll(target=VERSION, identity=IDENTITY):
    """The certified evidence for the round, shaped as the LL really is."""
    manifests = {"version": {}}
    if identity is not None:
        manifests["version"]["identity"] = identity
    return types.SimpleNamespace(ll={"release": {
        "target": target,
        "remote_publication": {"manifests": manifests},
    }})


def _install(monkeypatch, *, manifest=MANIFEST,
             digest=DIGEST, registry_rc=0, local_rc=0):
    def fake_run(argv, **kwargs):
        if argv[:3] == ["docker", "manifest", "inspect"]:
            body = json.dumps([{"Descriptor": {"digest": digest},
                                "OCIManifest": manifest}])
            return types.SimpleNamespace(returncode=registry_rc, stdout=body,
                                         stderr="")
        if argv[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=local_rc, stdout="[]",
                                         stderr="")
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(PAGE.subprocess, "run", fake_run)


def test_a_proved_tag_is_pinned_to_its_digest(monkeypatch):
    """The count must come from `image@sha256:…`, not from a tag: a tag can be
    moved after the page is built, a digest cannot."""
    _install(monkeypatch)
    pinned, why = PAGE._certified_image_by_digest(_ll())
    assert why == ""
    assert pinned == f"{PAGE.release_program.RELEASE_IMAGE}@{DIGEST}"


def test_a_registry_manifest_that_is_not_the_recorded_one_is_refused(monkeypatch):
    """THE DIRECTION THAT MAKES THIS SAFE. If the tag now points at a different
    image than the release recorded, its tool count is not this page's count."""
    _install(monkeypatch, manifest={"schemaVersion": 2, "layers": []})
    pinned, why = PAGE._certified_image_by_digest(_ll())
    assert pinned is None
    assert "manifest identity" in why


@pytest.mark.parametrize("identity", [None, "not-a-sha", "b" * 63])
def test_a_release_without_a_usable_identity_is_refused(monkeypatch, identity):
    """Without the recorded identity there is nothing to compare the registry
    against, and a tag alone was never proof."""
    _install(monkeypatch)
    pinned, why = PAGE._certified_image_by_digest(_ll(identity=identity))
    assert pinned is None
    assert "identity" in why


def test_an_ll_without_a_published_version_is_refused(monkeypatch):
    _install(monkeypatch)
    pinned, why = PAGE._certified_image_by_digest(_ll(target=None))
    assert pinned is None
    assert "version" in why


def test_a_registry_that_does_not_answer_is_refused(monkeypatch):
    _install(monkeypatch, registry_rc=1)
    pinned, why = PAGE._certified_image_by_digest(_ll())
    assert pinned is None
    assert "registry" in why


def test_an_image_absent_locally_is_refused_rather_than_pulled(monkeypatch):
    """The image is ~31 GB. Building a page must never turn into a pull, and a
    count that needed one is a count this round did not have."""
    _install(monkeypatch, local_rc=1)
    pinned, why = PAGE._certified_image_by_digest(_ll())
    assert pinned is None
    assert "not present locally" in why


def test_the_one_image_name_comes_from_the_release_program():
    """`_image_ref` looked for `vibeic/vibeic-eda`, which nothing builds or
    pulls — every real tag carries the `ghcr.io/` prefix, so both candidates
    missed and the uncertified page's count was unmeasurable too."""
    source = (HERE / "build_page.py").read_text(encoding="utf-8")
    assert '"vibeic/vibeic-eda' not in source
    assert "f\"vibeic/vibeic-eda" not in source
    assert PAGE.release_program.RELEASE_IMAGE.startswith("ghcr.io/")
