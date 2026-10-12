"""Build-time identity guards; actual bundled source is tested in image smoke.

The tiny module below exercises Python's real eager annotation evaluation, not
KLayout. Its hashes are explicitly substituted for the image-source hashes.
"""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools/klayout_netlist_import_patch.py"
spec = importlib.util.spec_from_file_location("netlist_patch", HELPER)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

FIXTURE = b'''from typing import List
class Netlist:
    @property
    def all_cells(self) -> List[NetlistCell]:
        return self.cells
class NetlistCell:
    pass
'''


@pytest.fixture
def known_source(monkeypatch):
    patched = FIXTURE.replace(P.OLD, P.NEW)
    monkeypatch.setattr(P, "ORIGINAL_SHA256", hashlib.sha256(FIXTURE).hexdigest())
    monkeypatch.setattr(P, "PATCHED_SHA256", hashlib.sha256(patched).hexdigest())
    return FIXTURE, patched


def test_forward_reference_reproduces_and_only_annotation_changes(known_source):
    original, expected = known_source
    with pytest.raises(NameError, match="NetlistCell"):
        exec(original, {})
    actual = P.repair(original)
    assert actual == expected
    ns = {}
    exec(actual, ns)
    netlist = ns["Netlist"]()
    cell = ns["NetlistCell"]()
    netlist.cells = [cell]
    assert netlist.all_cells == [cell]


def test_install_is_idempotent_preserves_mode_and_owner(tmp_path, known_source):
    original, expected = known_source
    target = tmp_path / "netlist_parser.py"
    target.write_bytes(original)
    target.chmod(0o640)
    before = target.stat()
    assert P.install(target)["status"] == "patched"
    assert target.read_bytes() == expected
    first = target.stat()
    assert (first.st_mode, first.st_uid, first.st_gid) == (
        before.st_mode, before.st_uid, before.st_gid)
    assert P.install(target)["status"] == "already_patched"
    assert target.stat().st_mtime_ns == first.st_mtime_ns


@pytest.mark.parametrize("source", [b"", FIXTURE, FIXTURE.replace(P.OLD, P.NEW)])
def test_real_guard_refuses_unreviewed_sources_without_writing(tmp_path, source):
    target = tmp_path / "netlist_parser.py"
    target.write_bytes(source)
    with pytest.raises(ValueError, match="unrecognized"):
        P.install(target)
    assert target.read_bytes() == source


@pytest.mark.parametrize("source", [FIXTURE.replace(P.OLD, b""), FIXTURE + P.OLD])
def test_even_expected_input_digest_requires_one_patch_site(monkeypatch, source):
    monkeypatch.setattr(P, "ORIGINAL_SHA256", hashlib.sha256(source).hexdigest())
    with pytest.raises(ValueError, match="exactly one"):
        P.repair(source)


def test_output_digest_mismatch_cannot_write(tmp_path, known_source, monkeypatch):
    target = tmp_path / "netlist_parser.py"
    target.write_bytes(known_source[0])
    monkeypatch.setattr(P, "PATCHED_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="reviewed bytes"):
        P.install(target)
    assert target.read_bytes() == known_source[0]


def test_missing_file_and_symlink_fail_closed(tmp_path, known_source):
    target = tmp_path / "netlist_parser.py"
    with pytest.raises(ValueError, match="regular"):
        P.install(target)
    other = tmp_path / "other.py"
    other.write_bytes(known_source[0])
    target.symlink_to(other)
    with pytest.raises(ValueError, match="regular"):
        P.install(target)
    assert other.read_bytes() == known_source[0]


def test_compose_pins_the_helper_content():
    """A helper-only edit must also change the root compose fingerprint."""
    digest = hashlib.sha256(HELPER.read_bytes()).hexdigest()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert f"{digest}  /vibeic/klayout_netlist_import_patch.py" in dockerfile
    assert "!tools/klayout_netlist_import_patch.py" in (
        ROOT / ".dockerignore").read_text().splitlines()
