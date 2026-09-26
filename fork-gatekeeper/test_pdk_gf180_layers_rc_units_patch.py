"""gf180mcu LibreLane LAYERS_RC res is stated in ohm/um, the default Liberty's unit.

The volume's `libs.tech/librelane/config.tcl` carries the ORFS fit in kohm/um,
while OpenROAD reads it in the first Liberty's resistance unit and the default
gf180mcu_fd_sc_mcu7t5v0 Liberty is `1ohm`: wire R was ~1000x low.  The patch
rewrites the four exact Metal2-5 res lines x1000 and refuses whatever it cannot
prove: a second application, a changed line, and a default (or inheriting,
installed) library whose Liberty is not 1ohm.

These tests pin the TRANSFORM on the same shapes in miniature; they need no PDK.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "tools" / "pdk" / "pdk_gf180_layers_rc_units_patch.py"
_spec = importlib.util.spec_from_file_location("pdk_gf180_layers_rc_units_patch", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

SCL = "gf180mcu_fd_sc_mcu7t5v0"
CORNERS = ("tt_025C_5v00", "ff_n40C_5v50", "ss_125C_4v50")

CONFIG = (
    'if { ![info exist ::env(STD_CELL_LIBRARY)] } {\n'
    f'    set ::env(STD_CELL_LIBRARY) {SCL}\n'
    '}\n\n'
    'set ::env(CELL_LIBS) [dict create]\n'
    + "".join(
        f'dict set ::env(CELL_LIBS) *_{c} "\\\n'
        f'    $::env(PDK_ROOT)/$::env(PDK)/libs.ref/$::env(STD_CELL_LIBRARY)/lib/'
        f'$::env(STD_CELL_LIBRARY)__{c}.lib\\\n"\n' for c in CORNERS)
    + '\nset ::env(LAYERS_RC) [dict create]\n'
    '# RC fit from OpenROAD\n'
    'dict set ::env(LAYERS_RC) "*" Metal2 res 3.85861E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal2 cap 1.35357E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal3 res 2.06673E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal3 cap 1.46141E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal4 res 1.68609E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal4 cap 1.50688E-04\n'
    'dict set ::env(LAYERS_RC) "*" Metal5 res 7.92778E-05\n'
    'dict set ::env(LAYERS_RC) "*" Metal5 cap 1.55595E-04\n\n'
    'set ::env(VIAS_R) [dict create]\n'
    'dict set ::env(VIAS_R) "*" Via1 res 4.23\n'
    'dict set ::env(VIAS_R) "max_*" Via1 res 16.845\n'
)

OSU_CONFIG = (
    'set ::env(STD_CELL_LIBRARY) gf180mcu_osu_sc_gp9t3v3\n'
    'set ::env(LIB) [dict create]\n'
    'dict set ::env(LIB) *_tt_025C_3v30 "\\\n'
    '    $::env(PDK_ROOT)/$::env(PDK)/libs.ref/$::env(STD_CELL_LIBRARY)/lib/'
    '$::env(STD_CELL_LIBRARY)__tt_025C_3v30.lib\\\n"\n'
)


def _lib(unit):
    return f"library (x) {{\n  time_unit : \"1ns\" ;\n  pulling_resistance_unit : {unit} ;\n}}\n"


def _root(tmp_path, unit="1ohm", config=CONFIG, osu=None):
    root = tmp_path / "gf180mcuD"
    (root / "libs.tech/librelane").mkdir(parents=True)
    (root / mod.CONFIG).write_text(config)
    lib = root / "libs.ref" / SCL / "lib"
    lib.mkdir(parents=True)
    for c in CORNERS:
        (lib / f"{SCL}__{c}.lib").write_text(_lib(unit))
    if osu is not None:
        scl = "gf180mcu_osu_sc_gp9t3v3"
        (root / "libs.tech/librelane" / scl).mkdir()
        (root / "libs.tech/librelane" / scl / "config.tcl").write_text(osu)
        (root / "libs.ref" / scl / "lib").mkdir(parents=True)
        (root / "libs.ref" / scl / "lib" / f"{scl}__tt_025C_3v30.lib").write_text(_lib('"1kohm"'))
    return root


def test_the_four_res_lines_become_ohm_per_micron_and_nothing_else_moves(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0
    text = (root / mod.CONFIG).read_text()
    for layer, value in (("Metal2", "0.385861"), ("Metal3", "0.206673"),
                         ("Metal4", "0.168609"), ("Metal5", "0.0792778")):
        assert f'dict set ::env(LAYERS_RC) "*" {layer} res {value}\n' in text
    for old, new in mod.LINES:
        # x1000 exactly: a unit prefix, not a refit.
        assert abs(float(new.split()[-1]) / float(old.split()[-1]) - 1000) < 1e-9
    before, after = CONFIG.splitlines(), text.splitlines()
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert [a for a, _ in changed] == [old for old, _ in mod.LINES]
    # cap and VIAS_R are already in the Liberty's units.
    assert 'Metal2 cap 1.35357E-04' in text and 'Via1 res 4.23' in text


def test_a_second_application_is_refused_rather_than_multiplying_twice(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0
    before = (root / mod.CONFIG).read_text()
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.CONFIG).read_text() == before


def test_a_kohm_default_liberty_is_refused(tmp_path):
    root = _root(tmp_path, unit='"1kohm"')
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.CONFIG).read_text() == CONFIG


def test_a_missing_default_liberty_is_refused_not_assumed(tmp_path):
    root = _root(tmp_path)
    (root / "libs.ref" / SCL / "lib" / f"{SCL}__ss_125C_4v50.lib").unlink()
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.CONFIG).read_text() == CONFIG


def test_an_installed_kohm_library_that_inherits_the_values_is_refused(tmp_path):
    root = _root(tmp_path, osu=OSU_CONFIG)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.CONFIG).read_text() == CONFIG


def test_a_kohm_library_with_its_own_layers_rc_does_not_block(tmp_path):
    osu = OSU_CONFIG + 'set ::env(LAYERS_RC) [dict create]\n' \
                       'dict set ::env(LAYERS_RC) "*" Metal2 res 3.85861E-04\n'
    root = _root(tmp_path, osu=osu)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0


def test_a_kohm_pad_library_does_not_block(tmp_path):
    # gf180mcu_fd_io's Liberty is 1kohm, but PAD_LIBS come after the cell libs.
    root = _root(tmp_path)
    io = "gf180mcu_fd_io"
    (root / "libs.tech/librelane" / io).mkdir()
    (root / "libs.tech/librelane" / io / "config.tcl").write_text(
        'set ::env(PAD_LIBS) [dict create]\n'
        'dict set ::env(PAD_LIBS) *_tt_025C_5v00 "\\\n'
        '    [glob $::env(PDK_ROOT)/$::env(PDK)/libs.ref/$::env(PAD_CELL_LIBRARY)/lib/*__tt_025C_5v00.lib]\\\n"\n')
    (root / "libs.ref" / io / "lib").mkdir(parents=True)
    for c in CORNERS:
        (root / "libs.ref" / io / "lib" / f"{io}__{c}.lib").write_text(_lib('"1kohm"'))
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0


def test_a_changed_line_is_refused_rather_than_patched_elsewhere(tmp_path):
    root = _root(tmp_path, config=CONFIG.replace("Metal4 res 1.68609E-04", "Metal4 res 1.7E-04"))
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert "1.7E-04" in (root / mod.CONFIG).read_text()


def test_check_writes_nothing_and_a_wrong_expectation_fails(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--check"]) == 1
    assert (root / mod.CONFIG).read_text() == CONFIG
    assert mod.main(["--root", str(root), "--expect", "3"]) == 2
    assert (root / mod.CONFIG).read_text() == CONFIG


def test_the_step_is_declared_and_wired():
    manifest = json.loads((HERE.parent / "tools" / "pdk" / "local_mods.json").read_text())
    entry = [m for m in manifest["modifications"] if m["id"] == "gf180mcu-librelane-layers-rc-ohm"]
    assert len(entry) == 1
    entry = entry[0]
    assert entry["paths"] == ["gf180mcu/versions/*/gf180mcuD/libs.tech/librelane/config.tcl"]
    assert (entry["expect_modified"], entry["expect_added"], entry["expect_removed"]) == (1, 0, 0)
    step = "pdk_gf180_layers_rc_units_patch.py --root /foss/pdks/gf180mcuD --expect 4"
    assert entry["step"].endswith(step)
    dockerfile = (HERE.parent / "Dockerfile").read_text()
    assert dockerfile.count("pdk_gf180_layers_rc_units_patch.py") == 1
    at = dockerfile.index("pdk_gf180_layers_rc_units_patch.py")
    # In the gf180 RUN, after the Xyce patch, before that RUN's pycache sweep.
    assert dockerfile.index("pdk_gf180_xyce_05v0_patch.py") < at
    run_end = dockerfile.index("-name __pycache__", at)
    assert "\nRUN " not in dockerfile[at:run_end]
    assert "grep -cE '^dict set ::env\\(LAYERS_RC\\)" in dockerfile[at:run_end]
