"""The two halves of 0.3.47 that are about a PDK saying what it needs and the
image not having it.

vibeic-eda#170  gf180mcuD ships `libs.tech/klayout/tech/scripts/sealring.py`,
                which puts `.../tech/pymacros` on sys.path and does
                `from sealring_cells import gf180mcu_sealring`.  That package was
                not in the volume.  The script catches its own ImportError,
                prints "Error: Couldn't load the seal ring library." and calls
                `sys.exit()` -- WITH NO ARGUMENT, so the process exits 0.  A
                caller checking the exit status sees success and no GDS.
                Measured on the 0.3.46 image: the script exits 0 and writes
                nothing; on 0.3.47 the same call writes a 4.1 MB layout carrying
                64894 shapes over 14 layers.

vibeic-eda#172  ihp-sg13g2's own `libs.tech/ngspice/.spiceinit` ends with two
                `osdi` directives naming cap_cmomi.osdi and cap_cmomf.osdi, and
                neither existed in the image.  ngspice does not fail a run for an
                OSDI object it could not load, so the first symptom is a MoM
                capacitor that simply is not a device.

Both are the same shape of defect -- a requirement the PDK states in its own
files, that nothing measured -- so both checks are tested here together.

These tests build miniature PDK trees.  They need no PDK and no image: what they
pin is the DECISION each program makes, including the decisions that must stay
NEGATIVE.  A check that cannot fail is not a check, so for every green case below
there is a red one that differs by one thing.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PDK_TOOLS = HERE.parent / "tools" / "pdk"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, str(PDK_TOOLS / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


install_mod = _load("pdk_gf180_sealring_install")
osdi_mod = _load("pdk_osdi_refs_check")
seal_mod = _load("pdk_sealring_check")

CONSUMER = '''\
import os, sys
sys.path.insert(1, os.path.join(os.getenv("PDK_ROOT"), os.getenv("PDK"),
                                "libs.tech", "klayout", "tech", "pymacros"))
try:
    from sealring_cells import gf180mcu_sealring
    gf180mcu_sealring()
except ImportError:
    print("Error: Couldn't load the seal ring library.")
    sys.exit()
'''


def _pdk(root: Path, name: str = "gf180mcuD", consumer: str | None = CONSUMER) -> Path:
    tech = root / name / "libs.tech" / "klayout" / "tech"
    (tech / "pymacros").mkdir(parents=True)
    if consumer is not None:
        (tech / "scripts").mkdir(parents=True)
        (tech / "scripts" / "sealring.py").write_text(consumer)
    return root / name


def _source(tmp: Path, n: int = 4, with_init: bool = True) -> Path:
    src = tmp / "src"
    src.mkdir(parents=True)
    if with_init:
        src.joinpath("__init__.py").write_text(
            "class gf180mcu_sealring:\n    def __init__(self):\n        self.ok = True\n")
        n -= 1
    for i in range(n):
        src.joinpath(f"m{i}.py").write_text("# filler\n")
    return src


# --------------------------------------------------------------------------
# pdk_gf180_sealring_install.py
# --------------------------------------------------------------------------

def test_install_places_exactly_the_declared_files(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root)
    got = install_mod.install(root, "gf180mcuD", _source(tmp_path), expect=4)
    dest = root / "gf180mcuD/libs.tech/klayout/tech/pymacros/sealring_cells"
    assert sorted(p.name for p in dest.glob("*.py")) == got
    assert len(got) == 4


def test_install_refuses_when_the_volume_already_has_the_package(tmp_path):
    """A silent overwrite would hide the volume growing its own copy."""
    root = tmp_path / "pdks"
    pdk = _pdk(root)
    (pdk / "libs.tech/klayout/tech/pymacros/sealring_cells").mkdir()
    with pytest.raises(install_mod.InstallError, match="already exists"):
        install_mod.install(root, "gf180mcuD", _source(tmp_path), expect=4)


def test_install_refuses_when_nothing_would_import_it(tmp_path):
    """No consumer script -> installing the library leaves dead code behind."""
    root = tmp_path / "pdks"
    _pdk(root, consumer=None)
    with pytest.raises(install_mod.InstallError, match="no seal-ring script|absent"):
        install_mod.install(root, "gf180mcuD", _source(tmp_path), expect=4)


def test_install_refuses_when_the_script_stopped_naming_this_package(tmp_path):
    """If upstream renames the import, this step must fail, not install a
    package nothing loads."""
    root = tmp_path / "pdks"
    _pdk(root, consumer="from something_else import thing\n")
    with pytest.raises(install_mod.InstallError, match="does not contain"):
        install_mod.install(root, "gf180mcuD", _source(tmp_path), expect=4)


def test_install_expect_count_fails_in_both_directions(tmp_path):
    for n, expect in ((4, 5), (4, 3)):
        root = tmp_path / f"pdks{n}{expect}"
        _pdk(root)
        with pytest.raises(install_mod.InstallError, match="expected"):
            install_mod.install(root, "gf180mcuD", _source(tmp_path / f"s{expect}"), expect=expect)


def test_install_refuses_a_source_that_is_not_a_package(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root)
    with pytest.raises(install_mod.InstallError, match="not a package"):
        install_mod.install(root, "gf180mcuD", _source(tmp_path, with_init=False), expect=3)


# --------------------------------------------------------------------------
# pdk_osdi_refs_check.py
# --------------------------------------------------------------------------

def _ngspice(root: Path, name: str, spiceinit: str) -> Path:
    d = root / name / "libs.tech" / "ngspice"
    (d / "osdi").mkdir(parents=True)
    (d / ".spiceinit").write_text(spiceinit)
    return d


def test_osdi_missing_object_is_found_and_named(tmp_path):
    root = tmp_path / "pdks"
    _ngspice(root, "p", "osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/cap_cmomi.osdi'\n")
    missing = osdi_mod.check_pdk(root, "p")
    assert len(missing) == 1
    assert missing[0][3].endswith("/p/libs.tech/ngspice/osdi/cap_cmomi.osdi")


def test_osdi_present_object_is_not_reported(tmp_path):
    """The same tree, differing only by the file existing. This is the pair that
    makes the test above mean something."""
    root = tmp_path / "pdks"
    d = _ngspice(root, "p", "osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/cap_cmomi.osdi'\n")
    (d / "osdi" / "cap_cmomi.osdi").write_bytes(b"\x7fELF")
    assert osdi_mod.check_pdk(root, "p") == []


def test_osdi_comment_lines_are_documentation_not_a_load(tmp_path):
    """cap_cmomi.lib quotes the directive in prose; that is not ngspice loading
    anything, and treating it as one would be a false red."""
    root = tmp_path / "pdks"
    _ngspice(root, "p", "* osdi $PDK_ROOT/$PDK/libs.tech/ngspice/osdi/nope.osdi\n")
    assert osdi_mod.check_pdk(root, "p") == []


def test_osdi_pre_osdi_directive_counts_too(tmp_path):
    root = tmp_path / "pdks"
    _ngspice(root, "p", "pre_osdi $PDK_ROOT/$PDK/libs.tech/ngspice/osdi/gone.osdi\n")
    assert len(osdi_mod.check_pdk(root, "p")) == 1


def test_osdi_expands_pdk_per_family_not_from_the_environment(tmp_path, monkeypatch):
    """The answer must not depend on which PDK the caller happened to select."""
    monkeypatch.setenv("PDK", "some-other-pdk")
    root = tmp_path / "pdks"
    _ngspice(root, "p", "osdi '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/x.osdi'\n")
    missing = osdi_mod.check_pdk(root, "p")
    assert "/p/" in missing[0][3] and "some-other-pdk" not in missing[0][3]


def test_osdi_non_osdi_targets_are_ignored(tmp_path):
    root = tmp_path / "pdks"
    _ngspice(root, "p", "osdi $PDK_ROOT/$PDK/libs.tech/ngspice/osdi/notmodel.txt\n")
    assert osdi_mod.check_pdk(root, "p") == []


# --------------------------------------------------------------------------
# pdk_sealring_check.py
# --------------------------------------------------------------------------

def test_sealring_package_shape_passes_when_the_library_constructs(tmp_path):
    root = tmp_path / "pdks"
    pdk = _pdk(root)
    dest = pdk / "libs.tech/klayout/tech/pymacros/sealring_cells"
    dest.mkdir()
    (dest / "__init__.py").write_text(
        "class gf180mcu_sealring:\n    def __init__(self):\n        pass\n")
    verdict, detail = seal_mod.check_pdk(root, "gf180mcuD")
    assert verdict == "PASS", detail


def test_sealring_package_shape_fails_when_the_package_is_absent(tmp_path):
    """This is exactly 0.3.46's gf180mcuD."""
    root = tmp_path / "pdks"
    _pdk(root)
    verdict, detail = seal_mod.check_pdk(root, "gf180mcuD")
    assert verdict == "FAIL"
    assert "sealring_cells" in detail


def test_sealring_package_that_imports_but_cannot_build_still_fails(tmp_path):
    """A directory of .py files that imports and then raises would satisfy a
    naive import check and still draw nothing."""
    root = tmp_path / "pdks"
    pdk = _pdk(root)
    dest = pdk / "libs.tech/klayout/tech/pymacros/sealring_cells"
    dest.mkdir()
    (dest / "__init__.py").write_text(
        "class gf180mcu_sealring:\n"
        "    def __init__(self):\n"
        "        raise RuntimeError('no PCell')\n")
    verdict, _ = seal_mod.check_pdk(root, "gf180mcuD")
    assert verdict == "FAIL"


def test_sealring_absent_script_is_not_a_failure(tmp_path):
    """Not every PDK carries a seal ring; inventing a requirement is a false red."""
    root = tmp_path / "pdks"
    _pdk(root, name="nangate45", consumer=None)
    assert seal_mod.check_pdk(root, "nangate45")[0] == "N/A"


def test_sealring_unknown_shape_is_not_measured_never_failed(tmp_path):
    """'I do not know how to check this' and 'this is broken' are different
    sentences. An earlier draft of this check conflated them and called both ihp
    families FAIL for a property of the check."""
    root = tmp_path / "pdks"
    _pdk(root, consumer="print('a seal ring, somehow')\n")
    verdict, detail = seal_mod.check_pdk(root, "gf180mcuD")
    assert verdict == "NOT_MEASURED", detail


def test_sealring_library_shape_without_a_technology_is_not_measured(tmp_path):
    """The ihp shape: LIB/PCELL constants, run under `klayout -n <tech>`. With no
    .lyt naming a technology the honest answer is NOT_MEASURED."""
    root = tmp_path / "pdks"
    _pdk(root, name="ihp-like",
         consumer="import pya\nLIB = 'SG13_dev'\nPCELL = 'sealring'\n")
    verdict, detail = seal_mod.check_pdk(root, "ihp-like")
    assert verdict == "NOT_MEASURED"
    assert "SG13_dev" in detail


def test_sealring_library_shape_is_recognised_as_the_library_shape(tmp_path):
    """With a .lyt present the check must take the library route rather than
    falling through to 'unknown shape'."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, name="ihp-like",
               consumer="import pya\nLIB = 'SG13_dev'\nPCELL = 'sealring'\n")
    (pdk / "libs.tech/klayout/tech/t.lyt").write_text("<technology><name>tt</name></technology>")
    assert seal_mod._tech_name(pdk) == "tt"
    verdict, detail = seal_mod.check_pdk(root, "ihp-like")
    assert verdict in ("PASS", "FAIL", "NOT_MEASURED")
    assert "unknown" not in detail.lower()


def test_sealring_required_family_that_is_absent_fails(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root, name="nangate45", consumer=None)
    rc = seal_mod.main(["--pdk-root", str(root), "--pdk", "nangate45",
                        "--require", "nangate45"])
    assert rc == 1


# --------------------------------------------------------------------------
# the manifest entry must describe a step that exists
# --------------------------------------------------------------------------

def test_local_mods_declares_the_sealring_step_additively():
    manifest = json.loads((PDK_TOOLS / "local_mods.json").read_text())
    entry = next(m for m in manifest["modifications"]
                 if m["id"] == "gf180mcu-sealring-cells-library")
    assert entry["expect_added"] == 4
    assert entry["expect_modified"] == 0, "this step must not touch delivered bytes"
    assert entry["expect_removed"] == 0
    prog = entry["step"].split()[0]
    assert (PDK_TOOLS.parent.parent / prog).is_file(), f"{prog} named but absent"
    assert len(list((PDK_TOOLS / "sealring_cells").glob("*.py"))) == entry["expect_added"]


def test_every_new_pdk_program_runs_as_a_script():
    """Each is invoked from the Dockerfile; a syntax error there is a broken
    build, not a broken test."""
    for name in ("pdk_gf180_sealring_install", "pdk_osdi_refs_check", "pdk_sealring_check"):
        proc = subprocess.run([sys.executable, str(PDK_TOOLS / f"{name}.py"), "--help"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{name} --help failed: {proc.stderr}"
