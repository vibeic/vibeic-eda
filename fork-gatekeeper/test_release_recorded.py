"""vibeic-eda#51 — a published version must be RECORDED as published.

`RELEASED.json` states its own contract: "a pin set not matching this has never
been released, however current the pins look." 0.2.53 was cut BY HAND in
response to #45/#46 — VERSION advanced, the image was built, pushed and verified
— and the ledger still described 0.2.52. Measured:

    VERSION on main                     0.2.53
    RELEASED.json "version"             0.2.52
    main tools/openroad/Dockerfile      OPENROAD_REF=47636465f9…
    RELEASED.json openroad pin          09d67f08f8…
    the published 0.2.53 image        EXISTS, and its own
      /vibeic/provenance/openroad.json  {"ref":"47636465f969…"}

The delivery was complete and only the record was missing — but `daily_release`
decides what to build by comparing fingerprints, so the next tick would have
published 0.2.54 byte-identical to 0.2.53.

THE SHAPE, and this repo has already had its mirror image: the record is written
by ONE path and a release can happen by ANOTHER. #43 was the ledger claiming
shipped when nothing had; this is it claiming unshipped when something did.

TWO HALVES, because neither alone is enough. `write_released_record` is now the
single writer and `--record-release` lets a hand release call it — but no writer
can cover a path that never calls it, so `check_release_recorded` asks the
REGISTRY instead. The writer makes the right thing easy; the gate makes the
wrong thing visible.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from pathlib import Path

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import check_release_recorded as C  # noqa: E402
import daily_release as R  # noqa: E402


def _tree(tmp_path: Path, version="0.2.53") -> Path:
    """A miniature eda-root — the same shape `test_daily_release._tree` uses."""
    root = tmp_path / "eda"
    (root / "tools" / "yosys").mkdir(parents=True)
    (root / "tools" / "yosys" / "Dockerfile").write_text(
        "ARG YOSYS_REF=" + "a" * 40 + "\n"
        "RUN git clone https://github.com/vibeic/yosys.git /y\n")
    (root / "Dockerfile").write_text(
        "ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:aaaaaaa\n"
        "FROM ${IMG_YOSYS} AS img-yosys\n")
    (root / "docker-bake.hcl").write_text(
        'variable "YOSYS_REF" { default = "' + "a" * 40 + '" }\n'
        'target "yosys" {\n'
        '  context = "tools/yosys"\n'
        '  tags    = tool_tags("yosys", YOSYS_REF)\n'
        '}\n')
    (root / "VERSION").write_text(version + "\n")
    return root


def _record(root, version, fingerprint=None, pins=None):
    (root / "RELEASED.json").write_text(json.dumps(
        {"version": version,
         "pins_fingerprint": fingerprint if fingerprint is not None else "deadbeef",
         "pins": pins or {}}), encoding="utf-8")


# ── the writer ──────────────────────────────────────────────────────────────
def test_the_writer_records_a_fingerprint_the_reader_reproduces(tmp_path):
    """The 0.2.45 failure, from the other direction: a record whose fingerprint
    the tree does not recompute is unreproducible, so every later run reads
    "this pin set has never been released" and cuts another version."""
    root = _tree(tmp_path)
    rec = R.write_released_record(root, "0.2.53", R.bake_targets(root))
    targets = R.bake_targets(root)
    again = R.pins_fingerprint({
        **R.pinned_refs(root),
        **{f"recipe:{k}": R.recipe_hash(root, k) for k in targets},
        "recipe:__compose__": R.compose_recipe_hash(root)})
    assert rec["pins_fingerprint"] == again
    assert R.released_record(root)["version"] == "0.2.53"


def test_the_writer_records_the_trees_own_pins(tmp_path):
    root = _tree(tmp_path)
    rec = R.write_released_record(root, "0.2.53", R.bake_targets(root))
    assert rec["pins"] == {"yosys": "a" * 40}


def test_the_publish_path_uses_the_one_writer():
    """Two copies of this write is how the ledger and the release came to
    disagree. Pinned so the inline block cannot come back."""
    src = (_HERE / "daily_release.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert code.count('"RELEASED.json").write_text') == 1, (
        "RELEASED.json is written in more than one place again")
    assert code.count("write_released_record(") >= 3, (
        "the definition, the publish path, and --record-release")


# ── the hand-release entry ──────────────────────────────────────────────────
def _cli(root, *args):
    r = subprocess.run(
        [sys.executable, str(_HERE / "daily_release.py"),
         "--eda-root", str(root), *args],
        capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def test_record_release_writes_the_ledger(tmp_path):
    root = _tree(tmp_path)
    rc, out = _cli(root, "--record-release", "0.2.53")
    assert rc == 0, out
    assert R.released_record(root)["version"] == "0.2.53"


def test_record_release_refuses_a_version_this_tree_is_not(tmp_path):
    """LOAD-BEARING. The record is computed FROM THE TREE, so stamping another
    version's name on it records a pin set that version was never built from —
    the #51 defect with the numbers swapped."""
    root = _tree(tmp_path, version="0.2.53")
    rc, out = _cli(root, "--record-release", "0.2.99")
    assert rc != 0, out
    assert "REFUSED" in out
    assert not (root / "RELEASED.json").exists(), "it wrote anyway"


# ── the gate ────────────────────────────────────────────────────────────────
def test_a_published_but_unrecorded_version_is_a_finding(tmp_path, monkeypatch):
    """THE #51 SHAPE, reproduced."""
    root = _tree(tmp_path)
    _record(root, "0.2.52")
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    verdict, findings, _s = C.audit(root)
    assert verdict == "FINDINGS", findings
    assert "0.2.52" in findings[0] and "IS published" in findings[0]
    assert "--record-release 0.2.53" in findings[0], (
        "a finding that does not say how to fix it makes the reader guess")


def test_recording_it_clears_the_finding(tmp_path, monkeypatch):
    """THE ACCEPT CASE, and it goes through the real writer rather than a
    hand-built record — a fingerprint the gate would reject is exactly what a
    second implementation produces."""
    root = _tree(tmp_path)
    R.write_released_record(root, "0.2.53", R.bake_targets(root))
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    assert C.audit(root)[0] == "OK"


def test_an_unpublished_version_is_not_a_finding(tmp_path, monkeypatch):
    """The normal mid-release state: VERSION bumped, nothing published yet. The
    ledger is CORRECT to still name the previous release, and a gate that fails
    here would be switched off within a day."""
    root = _tree(tmp_path)
    _record(root, "0.2.52")
    monkeypatch.setattr(C, "published", lambda *a, **k: False)
    verdict, findings, _s = C.audit(root)
    assert verdict == "OK", findings


def test_an_unreproducible_fingerprint_is_a_finding(tmp_path, monkeypatch):
    """The 0.2.45 failure. The version matches and the record is still useless:
    the reader recomputes a different digest and concludes nothing has shipped."""
    root = _tree(tmp_path)
    _record(root, "0.2.53", fingerprint="notthedigest")
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    verdict, findings, _s = C.audit(root)
    assert verdict == "FINDINGS"
    assert any("UNREPRODUCIBLE" in f or "unreproducible" in f.lower()
               for f in findings), findings


def test_an_unaskable_registry_is_not_a_pass(tmp_path, monkeypatch):
    """A registry error read as "the tag does not exist" turns an outage into a
    clean bill of health for an unrecorded release."""
    root = _tree(tmp_path)
    _record(root, "0.2.52")
    monkeypatch.setattr(C, "published", lambda *a, **k: None)
    verdict, findings, _s = C.audit(root)
    assert verdict == "CANNOT_ASK", (verdict, findings)
    assert "UNCHECKED, not confirmed" in findings[0]


def test_a_missing_version_file_is_not_a_pass(tmp_path):
    root = _tree(tmp_path)
    (root / "VERSION").unlink()
    assert C.audit(root)[0] == "CANNOT_ASK"


def test_published_separates_a_real_absence_from_a_failed_ask(monkeypatch):
    """`published` returns three values, and the middle one is what keeps an
    outage from reading as "not released yet"."""
    class R0:
        returncode, stdout, stderr = 0, "{}", ""

    class RMissing:
        returncode, stdout, stderr = 1, "", "manifest unknown"

    class RBroken:
        returncode, stdout, stderr = 1, "", "unauthorized: authentication required"

    for cls, want in ((R0, True), (RMissing, False), (RBroken, None)):
        monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: cls())
        assert C.published("0.0.1") is want, cls.__name__


def test_the_gate_returns_the_documented_exit_codes(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    _record(root, "0.2.52")
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    assert C.main(["--eda-root", str(root)]) == C.RC_FINDINGS
    monkeypatch.setattr(C, "published", lambda *a, **k: None)
    assert C.main(["--eda-root", str(root)]) == C.RC_CANNOT_ASK
    R.write_released_record(root, "0.2.53", R.bake_targets(root))
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    assert C.main(["--eda-root", str(root)]) == C.RC_OK


# ── the branch that SUPPRESSES the finding had no test at all ───────────────
#
# `pins_moved` decides between "nothing is wrong, the pins advanced since the
# release" and "this record cannot be re-derived". Only the second had a test,
# and it was red. A suppressing branch with no test is how an empty pin map came
# to read as PINS_AHEAD: `{}` compares unequal to any real tree, so the record
# most likely to be broken took the branch that says nothing is wrong.

def test_an_EMPTY_recorded_pin_map_cannot_claim_the_pins_moved(tmp_path, monkeypatch):
    """No pins recorded means we cannot tell — which is not the same as fine.

    A record written by a writer predating the `pins` field, or a truncated one,
    carries `{}`. That is precisely the record most likely to be unreproducible,
    and it was the one the UNREPRODUCIBLE check could never reach."""
    root = _tree(tmp_path)
    _record(root, "0.2.53", fingerprint="notthedigest", pins={})
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    verdict, findings, stats = C.audit(root)
    assert stats.get("pins_moved") is not True, (
        "an empty pin map was read as evidence that the pins moved")
    assert verdict == "FINDINGS", (verdict, findings, stats)


def test_a_REAL_pin_advance_still_reads_as_PINS_AHEAD(tmp_path, monkeypatch):
    """The guard on the fix above: it must not turn every mismatch into a finding.

    Between a release and the next one the pins legitimately move, and calling
    that a broken record leaves the tick red for the whole interval — the
    regression observed the day #71 landed."""
    root = _tree(tmp_path)
    _record(root, "0.2.53", fingerprint="notthedigest",
            pins={"yosys": "b" * 40})          # non-empty AND different
    monkeypatch.setattr(C, "published", lambda *a, **k: True)
    verdict, findings, stats = C.audit(root)
    assert stats.get("pins_moved") is True, stats
    assert verdict != "FINDINGS", (verdict, findings)
    assert "PINS_AHEAD" in (stats.get("note") or ""), stats
