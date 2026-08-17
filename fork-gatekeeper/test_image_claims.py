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
from types import SimpleNamespace

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


# --- the exit code is what the tick and CI read, and nothing tested it

def test_a_finding_produces_a_failing_exit_code(monkeypatch, tmp_path):
    """Every test above asserts what `pdk_findings()` RETURNS. None asserted
    what `main()` EXITS with — and the caller reads only the exit code.

    Measured: replacing this program's `return RC_DRIFT` with `RC_OK` left the
    whole suite green. Six tests over a gate that had stopped gating, which is
    the same shape as the guard whose three property tests missed an
    unreachable `if found:` one commit ago.
    """
    decl = [{"name": "sky130A", "upstream": "u/x", "status": "upstream",
             "arrives": "base", "version_file": "SOURCES",
             "version_pattern": r"open_pdks [0-9a-f]{40}", "read_by": "DRC"}]
    monkeypatch.setattr(K, "declared", lambda root=None: decl)
    monkeypatch.setattr(K, "pdk_findings", lambda img, d: [
        {"kind": "undeclared", "pdk": "ghost", "problem": "ships undeclared"}])
    monkeypatch.setattr(K, "manifest_findings", lambda img: [])
    assert K.main(["img"]) == K.RC_DRIFT


def test_a_clean_image_produces_a_passing_exit_code(monkeypatch):
    """…and the other direction, or the test above is met by always failing."""
    decl = [{"name": "sky130A", "upstream": "u/x", "status": "upstream",
             "arrives": "base", "version_file": "SOURCES",
             "version_pattern": r"open_pdks [0-9a-f]{40}", "read_by": "DRC"}]
    monkeypatch.setattr(K, "declared", lambda root=None: decl)
    monkeypatch.setattr(K, "pdk_findings", lambda img, d: [
        {"kind": "ok", "pdk": "sky130A", "read": "open_pdks " + "a" * 40,
         "upstream": "u/x", "status": "upstream"}])
    monkeypatch.setattr(K, "manifest_findings", lambda img: [])
    assert K.main(["img"]) == K.RC_OK


def test_no_declaration_refuses_rather_than_passing(monkeypatch):
    """An empty declaration compared nothing; that must not read as clean."""
    monkeypatch.setattr(K, "declared", lambda root=None: [])
    assert K.main(["img"]) == K.RC_NOTHING


# --- progress-supervised probes: incomplete work is not an image finding

def test_docker_probe_uses_activity_lease_without_a_total_ceiling(monkeypatch):
    seen = {}

    def run(image, script, stall_after):
        seen.update(image=image, script=script, stall_after=stall_after)
        return SimpleNamespace(rc=0, output="measured", outcome="natural")

    monkeypatch.setattr(K, "run_container_probe", run)
    got = K._sh(["docker", "run", "--rm", "--entrypoint", "bash",
                 "example:image", "-lc", "real-work"])

    assert got == K.ProbeRun(0, "measured", "", "natural")
    assert seen == {"image": "example:image", "script": "real-work",
                    "stall_after": K.PROBE_STALL_GRACE_S}


def test_stalled_version_read_is_unknown_not_no_version(monkeypatch):
    def run(cmd, timeout=None):
        del timeout
        if "ls -1 /foss/pdks" in cmd[-1]:
            return K.ProbeRun(0, "sky130A\n", "", "natural")
        return K.ProbeRun(199, "", "no progress", "stalled")

    monkeypatch.setattr(K, "_sh", run)
    rows = K.pdk_findings("img", DECL)

    assert len(rows) == 1
    assert rows[0]["kind"] == "not_checked"
    assert rows[0]["pdk"] == "sky130A"
    assert rows[0]["probe_outcome"] == "stalled"
    assert not [row for row in rows if row["kind"] == "no_version"]


def test_failed_version_read_rc_is_unknown_not_no_version(monkeypatch):
    """The old caller discarded this rc and emitted a false ``no_version``."""
    def run(cmd, timeout=None):
        del timeout
        if "ls -1 /foss/pdks" in cmd[-1]:
            return 0, "sky130A\n", ""
        return 73, "", "container-side read failed"

    monkeypatch.setattr(K, "_sh", run)
    rows = K.pdk_findings("img", DECL)

    assert len(rows) == 1
    assert rows[0]["kind"] == "not_checked"
    assert rows[0]["probe_rc"] == 73
    assert "container-side read failed" in rows[0]["problem"]


def test_failed_tool_version_probe_is_retained_as_unknown(monkeypatch):
    monkeypatch.setattr(K, "_TOOL_PROBE", {
        "yosys": ("yosys --version", r"Yosys ([0-9.]+)")})

    def run(cmd, timeout=None):
        del timeout
        script = cmd[-1]
        if "/foss/pdks/versions.txt" in script:
            return 0, "yosys 0.50\n", ""
        return 0, "could not start\n__VIBEIC_PROBE_RC__127\n", ""

    monkeypatch.setattr(K, "_sh", run)
    rows = K.manifest_findings("img")

    assert rows == [{
        "kind": "not_checked", "tool": "yosys",
        "probe_outcome": "natural", "probe_rc": 127,
        "problem": "`yosys --version` exited 127; no version was measured",
    }]


def test_unknown_probe_makes_main_norecord_and_json_keeps_reason(
        monkeypatch, tmp_path):
    monkeypatch.setattr(K, "declared", lambda root=None: DECL)
    unknown = {"kind": "not_checked", "pdk": "sky130A",
               "probe_outcome": "launch_error", "probe_rc": 127,
               "problem": "container never launched"}
    monkeypatch.setattr(K, "pdk_findings", lambda image, decl: [unknown])
    monkeypatch.setattr(K, "manifest_findings", lambda image: [])
    report = tmp_path / "claims.json"

    assert K.main(["img", "--json", str(report)]) == K.RC_NOTHING
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["pdks"] == [unknown]
