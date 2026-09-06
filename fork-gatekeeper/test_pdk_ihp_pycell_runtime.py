"""vibeic-eda#173: the ihp PCell runtime install, the PCell-library check, and
the vendored tree's provenance record.

The install is exercised against the REAL vendored tree (37 files) on a fake
PDK, so `--expect` in Dockerfile.derived and the tree on disk cannot disagree
without a red test.  The check is exercised with a fake `klayout` on PATH that
runs the probe under a stub `pya`, because the true library needs KLayout to
execute; the true execution is the container arm, measured in the build.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PDK_TOOLS = HERE.parent / "tools" / "pdk"
RUNTIME = PDK_TOOLS / "ihp_pycell_runtime"
EXPECT = 37   # the number Dockerfile.derived passes as --expect


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, str(PDK_TOOLS / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


install_mod = _load("pdk_ihp_pycell_runtime_install")
check_mod = _load("pdk_pycell_lib_check")

BOOTSTRAP = """\
import sys, os
base_dir = os.path.dirname(__file__)
sys.path.append(os.path.join(base_dir, '..', '..', 'python'))
sys.path.append(os.path.join(base_dir, '..', '..', 'python', 'pycell4klayout-api', 'source', 'python'))
"""

PYCELL_INIT = """\
from cni.tech import Tech
from cni.dlo import PCellWrapper
from pypreprocessor.pypreprocessor import preprocessor as preProcessor
"""

LYT = "<technology><name>sg13g2</name></technology>\n"


def _pdk(root: Path, name: str = "ihp-sg13g2", *, bootstrap: str | None = BOOTSTRAP,
         pycell_init: str | None = PYCELL_INIT, lyt: str | None = LYT,
         empty_runtime_dirs: bool = True) -> Path:
    kl = root / name / "libs.tech" / "klayout"
    (kl / "tech" / "pymacros").mkdir(parents=True)
    if bootstrap is not None:
        (kl / "tech" / "pymacros" / "autorun.lym").write_text(bootstrap)
    if lyt is not None:
        (kl / "tech" / f"{name}.lyt").write_text(lyt)
    (kl / "python").mkdir()
    if pycell_init is not None:
        lib = kl / "python" / "sg13g2_pycell_lib"
        lib.mkdir()
        (lib / "__init__.py").write_text(pycell_init)
    if empty_runtime_dirs:
        (kl / "python" / "pycell4klayout-api").mkdir()
        (kl / "python" / "pypreprocessor").mkdir()
    return root / name


# --------------------------------------------------------------------------
# provenance: the vendored tree is what PROVENANCE.md says it is
# --------------------------------------------------------------------------

def test_manifest_matches_the_tree_in_both_directions():
    """Every vendored file is in MANIFEST.sha256 with its current digest, and
    every manifest line names a file that exists.  A file added, dropped or
    edited without re-recording provenance is a red test, not a surprise."""
    listed = {}
    for line in (RUNTIME / "MANIFEST.sha256").read_text().splitlines():
        sha256, _blob, rel = line.split("  ")
        listed[rel] = sha256
    on_disk = sorted(
        str(p.relative_to(RUNTIME))
        for sub in ("pycell4klayout-api", "pypreprocessor")
        for p in (RUNTIME / sub).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert on_disk == sorted(listed), "tree and MANIFEST.sha256 name different files"
    for rel, sha256 in listed.items():
        assert hashlib.sha256((RUNTIME / rel).read_bytes()).hexdigest() == sha256, rel
    assert len(on_disk) == EXPECT


def test_the_tree_carries_what_the_shipped_library_imports():
    """cni.tech, cni.dlo, pypreprocessor.pypreprocessor -- the three imports in
    the shipped sg13g2_pycell_lib/__init__.py -- resolve by file in the tree."""
    for dotted in ("cni.tech", "cni.dlo", "pypreprocessor.pypreprocessor"):
        assert install_mod._module_file(RUNTIME, dotted) is not None, dotted
    for lic in ("pycell4klayout-api/LICENSE", "pypreprocessor/LICENSE"):
        assert (RUNTIME / lic).is_file(), lic


# --------------------------------------------------------------------------
# pdk_ihp_pycell_runtime_install.py
# --------------------------------------------------------------------------

def test_install_places_exactly_the_declared_files(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root)
    got = install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)
    assert len(got) == EXPECT
    py = root / "ihp-sg13g2/libs.tech/klayout/python"
    assert (py / "pycell4klayout-api/source/python/cni/tech.py").is_file()
    assert (py / "pypreprocessor/pypreprocessor/__init__.py").is_file()
    assert (py / "pycell4klayout-api/LICENSE").is_file()


def test_install_reads_the_imports_out_of_the_library():
    """The install target list is the library's own `from X import` lines."""
    mods = install_mod.imported_runtime_modules(
        RUNTIME.parent / "ihp_pycell_runtime" / "MANIFEST.sha256")  # not a library
    assert mods == []
    tmp = RUNTIME.parent / "_probe_init.py"
    try:
        tmp.write_text(PYCELL_INIT + "from os import path\n")
        assert install_mod.imported_runtime_modules(tmp) == [
            "cni.tech", "cni.dlo", "pypreprocessor.pypreprocessor"]
    finally:
        tmp.unlink()


def test_install_refuses_when_the_volume_already_has_the_runtime(tmp_path):
    """A silent overwrite would hide the volume growing its own copy."""
    root = tmp_path / "pdks"
    pdk = _pdk(root)
    (pdk / "libs.tech/klayout/python/pycell4klayout-api/README").write_text("upstream's\n")
    with pytest.raises(install_mod.InstallError, match="not empty"):
        install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)


def test_install_refuses_the_family_that_borrows_the_runtime(tmp_path):
    """ihp-sg13cmos5l's runtime dirs are symlinks into ihp-sg13g2.  Installing
    into the borrower would either fail or shadow the owner; refuse and say so."""
    root = tmp_path / "pdks"
    _pdk(root, "ihp-sg13g2")
    b = _pdk(root, "ihp-sg13cmos5l", empty_runtime_dirs=False)
    py = b / "libs.tech/klayout/python"
    for sub in ("pycell4klayout-api", "pypreprocessor"):
        (py / sub).symlink_to(f"../../../../ihp-sg13g2/libs.tech/klayout/python/{sub}")
    with pytest.raises(install_mod.InstallError, match="symlink|borrows"):
        install_mod.install(root, "ihp-sg13cmos5l", RUNTIME, expect=EXPECT)


def test_install_refuses_when_nothing_would_put_it_on_the_path(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root, bootstrap=None)
    with pytest.raises(install_mod.InstallError, match="absent"):
        install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)


def test_install_refuses_when_the_bootstrap_stopped_naming_the_path(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root, bootstrap="import sys\nsys.path.append('somewhere/else')\n")
    with pytest.raises(install_mod.InstallError, match="does not name"):
        install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)


def test_install_refuses_when_the_library_imports_none_of_it(tmp_path):
    """If upstream drops cni/pypreprocessor, this step must fail, not install
    a runtime nothing loads."""
    root = tmp_path / "pdks"
    _pdk(root, pycell_init="import pya\n")
    with pytest.raises(install_mod.InstallError, match="import none"):
        install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)


def test_install_refuses_a_source_missing_a_module_the_library_imports(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root, pycell_init=PYCELL_INIT + "from cni.does_not_exist import X\n")
    with pytest.raises(install_mod.InstallError, match="does not provide"):
        install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=EXPECT)


def test_install_expect_count_fails_in_both_directions(tmp_path):
    for expect in (EXPECT + 1, EXPECT - 1):
        root = tmp_path / f"pdks{expect}"
        _pdk(root)
        with pytest.raises(install_mod.InstallError, match="expected"):
            install_mod.install(root, "ihp-sg13g2", RUNTIME, expect=expect)


def test_install_cli_exit_codes(tmp_path):
    root = tmp_path / "pdks"
    _pdk(root)
    prog = str(PDK_TOOLS / "pdk_ihp_pycell_runtime_install.py")
    ok = subprocess.run([sys.executable, prog, "--pdk-root", str(root), "--pdk", "ihp-sg13g2",
                         "--source", str(RUNTIME), "--expect", str(EXPECT)],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert "[PASS]" in ok.stdout
    again = subprocess.run([sys.executable, prog, "--pdk-root", str(root), "--pdk", "ihp-sg13g2",
                            "--source", str(RUNTIME), "--expect", str(EXPECT)],
                           capture_output=True, text=True)
    assert again.returncode == 1
    assert "[FAIL]" in again.stderr


# --------------------------------------------------------------------------
# pdk_pycell_lib_check.py
# --------------------------------------------------------------------------

STUB_PYA = """\
import os, glob, re
def _tech_loaded(name):
    # a technology is loadable iff some *.lyt on KLAYOUT_PATH names it
    for d in os.environ.get("KLAYOUT_PATH", "").split(os.pathsep):
        for lyt in glob.glob(os.path.join(d, "*.lyt")) + glob.glob(os.path.join(d, "tech", "*.lyt")):
            if re.search("<name>%s</name>" % re.escape(name), open(lyt).read()):
                return True
    return False
class Technology:
    @staticmethod
    def has_technology(name): return _tech_loaded(name)
class _Layout:
    def pcell_declaration(self, name): return object()
class Library:
    _names = []            # (name, technology it was registered under)
    def __init__(self): pass
    def register(self, name): Library._names.append((name, os.environ.get("FAKE_TECH")))
    def layout(self): return _Layout()
    @staticmethod
    def library_names(): return [n for n, _ in Library._names]
    @staticmethod
    def library_by_name(name, technology=None):
        # KLayout: a technology-bound library is found only under its technology
        for n, t in Library._names:
            if n == name and (technology == t or t is None):
                return Library()
        return None
"""

FAKE_KLAYOUT = """\
#!/bin/sh
# fake klayout: -n <tech> -zz -r <probe>  -> run the probe under the stub pya,
# in the caller's environment (KLAYOUT_PATH included), with the tech recorded
KL="${{KLAYOUT_PATH%%:*}}"
export PYTHONPATH="{stub}:$KL/python:$KL/python/pycell4klayout-api/source/python:$PYTHONPATH"
export FAKE_TECH="$2"
echo "$KLAYOUT_PATH" > "{env_dump}"
exec {python} "$5"
"""


@pytest.fixture
def fake_klayout(tmp_path, monkeypatch):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "pya.py").write_text(STUB_PYA)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kl = bin_dir / "klayout"
    kl.write_text(FAKE_KLAYOUT.format(stub=stub, python=sys.executable,
                                      env_dump=tmp_path / "klayout_path.txt"))
    kl.chmod(kl.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return kl


REGISTERING_LIB = """\
import sys, os, pya
# the fake klayout runs no autorun.lym, so this stands in for the bootstrap
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(_here, "..", "pycell4klayout-api", "source", "python"))
from cni.tech import Tech   # resolves only once the runtime is installed
lib = pya.Library()
lib.register("SG13_dev")
lib2 = pya.Library()
lib2.register("SG13_native_pcell_lib")
"""


def _cni_stub(pdk: Path):
    """Enough of the runtime for the fake library's `from cni.tech import Tech`."""
    d = pdk / "libs.tech/klayout/python/pycell4klayout-api/source/python/cni"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tech.py").write_text("class Tech: pass\n")


def test_check_names_are_read_out_of_the_library():
    tmp = PDK_TOOLS / "_probe_names.py"
    try:
        tmp.write_text(REGISTERING_LIB)
        assert check_mod.registered_names(tmp) == ["SG13_dev", "SG13_native_pcell_lib"]
    finally:
        tmp.unlink()


def test_check_passes_when_every_named_library_registers(tmp_path, fake_klayout):
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=REGISTERING_LIB)
    _cni_stub(pdk)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "PASS", detail
    assert "SG13_dev" in detail


def test_check_fails_naming_the_missing_module_when_the_runtime_is_absent(tmp_path, fake_klayout):
    """This is exactly 0.3.46's ihp-sg13g2: the library imports cni, cni is an
    empty directory, SG13_dev never registers."""
    root = tmp_path / "pdks"
    _pdk(root, pycell_init=REGISTERING_LIB)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "FAIL"
    assert "ModuleNotFoundError" in detail and "cni" in detail


def test_check_fails_when_the_library_imports_but_a_named_library_never_registers(tmp_path, fake_klayout):
    """A library that imports cleanly and registers under a different name --
    or none -- would satisfy an import-only check and still be unusable."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=(
        "import pya\nfrom cni.tech import Tech\n"
        "if False:\n    pya.Library().register('SG13_dev')\n"
        "pya.Library().register('Something_else')\n"))
    _cni_stub(pdk)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "FAIL"
    assert "SG13_dev" in detail and "Something_else" in detail


def test_check_no_library_is_not_a_failure(tmp_path, fake_klayout):
    root = tmp_path / "pdks"
    _pdk(root, "nangate45", pycell_init=None, empty_runtime_dirs=False)
    assert check_mod.check_pdk(root, "nangate45")[0] == "N/A"


def test_check_without_a_technology_is_not_measured(tmp_path, fake_klayout):
    root = tmp_path / "pdks"
    _pdk(root, pycell_init=REGISTERING_LIB, lyt=None)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "NOT_MEASURED", detail


def test_check_without_klayout_is_not_measured(tmp_path, monkeypatch):
    root = tmp_path / "pdks"
    _pdk(root, pycell_init=REGISTERING_LIB)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "NOT_MEASURED"
    assert "klayout" in detail


def test_check_library_registering_nothing_nameable_is_not_measured(tmp_path, fake_klayout):
    root = tmp_path / "pdks"
    _pdk(root, pycell_init="import pya\n")
    assert check_mod.check_pdk(root, "ihp-sg13g2")[0] == "NOT_MEASURED"


def test_check_cli_require_turns_absence_into_failure(tmp_path, fake_klayout):
    root = tmp_path / "pdks"
    _pdk(root, "nangate45", pycell_init=None, empty_runtime_dirs=False)
    prog = str(PDK_TOOLS / "pdk_pycell_lib_check.py")
    lenient = subprocess.run([sys.executable, prog, "--pdk-root", str(root)],
                             capture_output=True, text=True)
    assert lenient.returncode == 0 and "[N/A]" in lenient.stdout
    strict = subprocess.run([sys.executable, prog, "--pdk-root", str(root), "--require", "nangate45"],
                            capture_output=True, text=True)
    assert strict.returncode == 1 and "[FAIL]" in strict.stdout
    nowhere = subprocess.run([sys.executable, prog, "--pdk-root", str(tmp_path / "nope")],
                             capture_output=True, text=True)
    assert nowhere.returncode == 2


def test_check_probe_runs_with_the_familys_own_klayout_path(tmp_path, fake_klayout):
    """`-n <tech>` resolves only if KLAYOUT_PATH carries the family. Measured:
    under the image's default environment `-n sg13cmos5l` answered with
    sg13g2's libraries. The probe must therefore be run in the family's own
    environment, whatever the caller's is."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=REGISTERING_LIB)
    _cni_stub(pdk)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "PASS", detail
    kl = pdk / "libs.tech" / "klayout"
    assert (tmp_path / "klayout_path.txt").read_text().strip() == f"{kl}{os.pathsep}{kl / 'tech'}"


def test_check_technology_that_does_not_load_is_not_measured(tmp_path, fake_klayout, monkeypatch):
    """The image's default environment carries ONE family on KLAYOUT_PATH.
    Asked for another, KLayout falls back silently. Run in such an environment
    the probe must report the technology unloaded, and the verdict must be
    NOT_MEASURED -- not PASS about the wrong family, not FAIL."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=REGISTERING_LIB)
    _cni_stub(pdk)
    other = _pdk(root, "other-family", pycell_init=None, empty_runtime_dirs=False,
                 lyt="<technology><name>other</name></technology>\n")
    def default_env(_pdk_dir):
        kl = other / "libs.tech" / "klayout"
        return {**os.environ, "KLAYOUT_PATH": f"{kl}{os.pathsep}{kl / 'tech'}"}
    monkeypatch.setattr(check_mod, "_family_env", default_env)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "NOT_MEASURED", detail
    assert "sg13g2" in detail and "did not load" in detail


def test_check_fails_when_klayout_could_not_read_the_librarys_own_files(tmp_path, fake_klayout):
    """ihp-sg13cmos5l in 0.3.46: SG13_dev registers, and KLayout's loader has
    just printed `ERROR: Reading .../ihp/guard_ring_code.py: Unable to open
    file` for two device modules that are dangling symlinks into a newer
    sg13g2. A library that registered with two of its modules unreadable is
    not a working library, and a check that said PASS would hide that."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=REGISTERING_LIB + (
        "import sys as _s\n"
        "_p = os.path.join(_here, 'ihp')\n"
        "print('ERROR: Reading ' + _p + '/guard_ring_code.py: Unable to open file: ' + _p + '//guard_ring_code.py (errno=2)', file=_s.stderr)\n"
        "print('ERROR: Reading ' + _p + '/device_base_code.py: Unable to open file (errno=2)', file=_s.stderr)\n"))
    _cni_stub(pdk)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "FAIL", detail
    assert "guard_ring_code.py" in detail and "device_base_code.py" in detail
    assert "SG13_dev" in detail   # it still registered, and the detail says so


def test_check_loader_errors_outside_the_library_are_not_its_failure(tmp_path, fake_klayout):
    """An ERROR about some other macro on the path is not this library's."""
    root = tmp_path / "pdks"
    pdk = _pdk(root, pycell_init=REGISTERING_LIB + (
        "import sys as _s\n"
        "print('ERROR: Reading /somewhere/else/macro.lym: Unable to open file', file=_s.stderr)\n"))
    _cni_stub(pdk)
    verdict, detail = check_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "PASS", detail


# --------------------------------------------------------------------------
# pdk_sealring_check.py -- the library shape, corrected (vibeic-eda#173)
# --------------------------------------------------------------------------

seal_mod = _load("pdk_sealring_check")

SEALRING_IHP = "import pya\nLIB = 'SG13_dev'\nPCELL = 'sealring'\n"


def _ihp_with_sealring(root: Path, name: str = "ihp-sg13g2") -> Path:
    pdk = _pdk(root, name, pycell_init=REGISTERING_LIB)
    _cni_stub(pdk)
    scripts = pdk / "libs.tech/klayout/tech/scripts"
    scripts.mkdir()
    (scripts / "sealring.py").write_text(SEALRING_IHP)
    # the fake klayout runs no autorun; import the library the way the bootstrap would
    (pdk / "libs.tech/klayout/tech/pymacros/autorun.lym").write_text(BOOTSTRAP)
    return pdk


def test_sealring_library_shape_asks_under_the_technology(tmp_path, fake_klayout, monkeypatch):
    """THE REGRESSION. A technology-bound library is None to
    library_by_name(name) alone. The old probe asked without the technology and
    called a working SG13_dev NOLIB; under the stub, which models KLayout's
    binding, the corrected probe finds it and its sealring PCell."""
    root = tmp_path / "pdks"
    pdk = _ihp_with_sealring(root)
    # the real autorun.lym imports the library before any -r script runs; the
    # fake klayout only builds the path, so import it in the probe's process
    probe_wrapper = tmp_path / "bin" / "klayout"
    probe_wrapper.write_text(probe_wrapper.read_text().replace(
        'exec {python} "$5"'.format(python=sys.executable),
        'exec {python} -c "import sys, sg13g2_pycell_lib; exec(open(sys.argv[1]).read())" "$5"'
        .format(python=sys.executable)))
    verdict, detail = seal_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "PASS", detail
    assert "sg13g2" in detail
    assert (tmp_path / "klayout_path.txt").read_text().strip().startswith(str(pdk / "libs.tech/klayout"))


def test_sealring_library_shape_technology_not_loaded_is_not_measured(tmp_path, fake_klayout, monkeypatch):
    """Same defect, same fix, in the sealring check: run under an environment
    that carries another family, `-n sg13g2` does not load and the honest
    verdict is NOT_MEASURED, where the old probe said FAIL about sg13g2 while
    looking at something else."""
    root = tmp_path / "pdks"
    _ihp_with_sealring(root)
    other = _pdk(root, "other-family", pycell_init=None, empty_runtime_dirs=False,
                 lyt="<technology><name>other</name></technology>\n")
    def default_env(_pdk_dir):
        kl = other / "libs.tech" / "klayout"
        return {**os.environ, "KLAYOUT_PATH": f"{kl}{os.pathsep}{kl / 'tech'}"}
    monkeypatch.setattr(seal_mod, "_family_env", default_env)
    verdict, detail = seal_mod.check_pdk(root, "ihp-sg13g2")
    assert verdict == "NOT_MEASURED", detail
    assert "sg13g2" in detail
