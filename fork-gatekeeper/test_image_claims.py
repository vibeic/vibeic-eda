#!/usr/bin/env python3
"""vibeic-eda#28 — the claims an image makes about itself, checked.

Three PDK upstreams were used by every sign-off run and declared nowhere, and
no existing guard could see them: they all check what we CLONE, and these
arrive pre-installed in the base image. This program is the jurisdiction that
was missing; these tests are what stop it becoming decorative.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_image_claims as K                                # noqa: E402


def _img(monkeypatch, files, listing=None):
    """Answer container reads from a fabricated /foss/pdks, banner included.

    The banner is deliberate: the base's profile.d prints three `[INFO]` lines
    to STDOUT on every login shell, and a login shell is required for the tools
    to be on PATH. Leaving it out of the fixture would test a container we do
    not have.
    """
    banner = "[INFO] Final PATH variable: /x\n[INFO] Final PYTHONPATH variable: /y\n"

    def run(cmd, timeout=600):
        script = cmd[-1]
        if "ls -1 /foss/pdks" in script:
            return 0, banner + "\n".join(listing or files) + "\n", ""
        for name, content in (files.items() if isinstance(files, dict) else []):
            if f"/foss/pdks/{name}/" in script:
                return 0, banner + content + "\n", ""
        return 0, banner, ""
    monkeypatch.setattr(K, "_sh", run)


DECL = [
    {"name": "sky130A", "upstream": "RTimothyEdwards/open_pdks", "status": "upstream",
     "arrives": "base", "version_file": "SOURCES",
     "version_pattern": r"open_pdks [0-9a-f]{40}", "read_by": "DRC/LVS"},
]


def test_the_banner_does_not_become_the_version(monkeypatch):
    """The first version of this program DISPLAYED `[INFO] Final PATH …`.

    Worse, the shape check still passed — the real line was further down the
    same blob, so the verdict was right for a reason that would evaporate the
    moment the input changed shape.
    """
    _img(monkeypatch, {"sky130A": "open_pdks " + "b" * 40})
    ok = [f for f in K.pdk_findings("img", DECL) if f["kind"] == "ok"]
    assert ok and ok[0]["read"].startswith("open_pdks "), \
        f"the reported value is not the file's content: {ok!r}"
    assert "[INFO]" not in ok[0]["read"]


def test_an_undeclared_pdk_is_a_finding(monkeypatch):
    """The defect this program exists for: a dependency nobody chose."""
    _img(monkeypatch, {"sky130A": "open_pdks " + "b" * 40},
         listing=["sky130A", "some-new-pdk"])
    kinds = {f["kind"]: f for f in K.pdk_findings("img", DECL)}
    assert "undeclared" in kinds
    assert kinds["undeclared"]["pdk"] == "some-new-pdk"


def test_a_declared_pdk_that_vanished_is_a_finding(monkeypatch):
    """Both directions: a flow will fail on a PDK the declaration promises."""
    _img(monkeypatch, {}, listing=["gf180mcuD"])
    kinds = {f["kind"] for f in K.pdk_findings("img", DECL)}
    assert "missing" in kinds and "undeclared" in kinds


def test_a_version_file_that_disappeared_is_a_finding(monkeypatch):
    """Not an error today, and it makes every future change invisible."""
    _img(monkeypatch, {"sky130A": ""}, listing=["sky130A"])
    kinds = {f["kind"] for f in K.pdk_findings("img", DECL)}
    assert "no_version" in kinds


def test_a_version_of_the_wrong_shape_is_a_finding(monkeypatch):
    """Guards the case the banner bug was hiding behind."""
    _img(monkeypatch, {"sky130A": "installed from somewhere"}, listing=["sky130A"])
    kinds = {f["kind"] for f in K.pdk_findings("img", DECL)}
    assert "version_shape" in kinds


def test_every_pdk_the_repository_declares_has_an_upstream_and_a_status():
    """A declaration with a blank owner is the state this issue is about."""
    for d in K.declared():
        assert d.get("upstream"), f"{d['name']} declares no upstream"
        assert d.get("status") in ("ours", "upstream"), \
            f"{d['name']} has no decision recorded — 'used and unowned' is the " \
            f"third state this file exists to make impossible"
        assert d.get("read_by"), f"{d['name']} does not say who reads it"
