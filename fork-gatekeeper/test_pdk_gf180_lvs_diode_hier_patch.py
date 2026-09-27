"""gf180mcu LVS: the diode_mk junction diodes are extracted in the cell that draws them.

With the run_mode=deep the PDK's LibreLane config declares, the shipped deck took
the WELL as one input of these diodes, so each one was extracted in the parent
cell (spm: 120 antenna diodes in spm, 0 in the antenna cell, a clean design
"Netlists don't match"). The patch gives each the junction's own footprint on its
well instead and connects that footprint to the well.

These tests pin the TRANSFORM on the deck's own text (verbatim excerpts of the
gf180mcuD volume the image ships); they need no PDK and no KLayout. The LVS
verdicts themselves are measured in the PR, not here.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "tools" / "pdk" / "pdk_gf180_lvs_diode_hier_patch.py"
_spec = importlib.util.spec_from_file_location("pdk_gf180_lvs_diode_hier_patch", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# Verbatim from the volume (open_pdks b344c97e), license headers dropped.
EXTRACTION = r"""
# diode_nd2ps_03v3 diode
logger.info('Extracting diode_nd2ps_03v3 diode')
extract_devices(diode('diode_nd2ps_03v3'), { 'N' => diode_nd2ps_03v3_terminal_n, 'P' => lvpwell_con })

# diode_nd2ps_03v3_dn diode
if CONSIDER_DN_DW_FEATURES
  logger.info('Extracting diode_nd2ps_03v3_dn diode')
  extract_devices(diode('diode_nd2ps_03v3_dn'), { 'N' => diode_nd2ps_03v3_dn_terminal_n, 'P' => lvpwell_con })
else
  logger.info('Extracting diode_nd2ps_03v3_dn diode as diode_nd2ps_03v3')
  extract_devices(diode('diode_nd2ps_03v3'), { 'N' => diode_nd2ps_03v3_dn_terminal_n, 'P' => lvpwell_con })
end

# diode_nd2ps_06v0 diode
logger.info('Extracting diode_nd2ps_06v0 diode')
extract_devices(diode('diode_nd2ps_06v0'), { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell_con })

# diode_nd2ps_06v0_dn diode
if CONSIDER_DN_DW_FEATURES
  logger.info('Extracting diode_nd2ps_06v0_dn diode')
  extract_devices(diode('diode_nd2ps_06v0_dn'), { 'N' => diode_nd2ps_06v0_dn_terminal_n, 'P' => lvpwell_con })
else
  logger.info('Extracting diode_nd2ps_06v0_dn diode as diode_nd2ps_06v0')
  extract_devices(diode('diode_nd2ps_06v0'), { 'N' => diode_nd2ps_06v0_dn_terminal_n, 'P' => lvpwell_con })
end

# diode_pd2nw_03v3 diode
logger.info('Extracting diode_pd2nw_03v3 diode')
extract_devices(diode('diode_pd2nw_03v3'), { 'N' => diode_pd2nw_03v3_terminal_n, 'P' => diode_pd2nw_03v3_terminal_p })

# diode_pd2nw_03v3_dn diode
if CONSIDER_DN_DW_FEATURES
  logger.info('Extracting diode_pd2nw_03v3_dn diode')
  extract_devices(diode('diode_pd2nw_03v3_dn'),
                  { 'N' => diode_pd2nw_03v3_dn_terminal_n, 'P' => diode_pd2nw_03v3_dn_terminal_p })
else
  logger.info('Extracting diode_pd2nw_03v3_dn diode as diode_pd2nw_03v3')
  extract_devices(diode('diode_pd2nw_03v3'),
                  { 'N' => diode_pd2nw_03v3_dn_terminal_n, 'P' => diode_pd2nw_03v3_dn_terminal_p })
end

# diode_pd2nw_06v0 diode
logger.info('Extracting diode_pd2nw_06v0 diode')
extract_devices(diode('diode_pd2nw_06v0'), { 'N' => diode_pd2nw_06v0_terminal_n, 'P' => diode_pd2nw_06v0_terminal_p })

# diode_pd2nw_06v0_dn diode
if CONSIDER_DN_DW_FEATURES
  logger.info('Extracting diode_pd2nw_06v0_dn diode')
  extract_devices(diode('diode_pd2nw_06v0_dn'),
                  { 'N' => diode_pd2nw_06v0_dn_terminal_n, 'P' => diode_pd2nw_06v0_dn_terminal_p })
else
  logger.info('Extracting diode_pd2nw_06v0_dn diode as diode_pd2nw_06v0')
  extract_devices(diode('diode_pd2nw_06v0'),
                  { 'N' => diode_pd2nw_06v0_dn_terminal_n, 'P' => diode_pd2nw_06v0_dn_terminal_p })
end

# diode_nw2ps_03v3 diode
logger.info('Extracting diode_nw2ps_03v3 diode')
extract_devices(diode('diode_nw2ps_03v3'), { 'N' => diode_nw2ps_03v3_terminal_n, 'P' => diode_nw2ps_03v3_terminal_p })

# diode_nw2ps_06v0 diode
logger.info('Extracting diode_nw2ps_06v0 diode')
extract_devices(diode('diode_nw2ps_06v0'), { 'N' => diode_nw2ps_06v0_terminal_n, 'P' => diode_nw2ps_06v0_terminal_p })

# diode_pw2dw_03v3 diode
logger.info('Extracting diode_pw2dw_03v3 diode')
extract_devices(diode('diode_pw2dw_03v3'), { 'N' => dnwell, 'P' => diode_pw2dw_03v3_terminal_p })

# diode_pw2dw_06v0 diode
logger.info('Extracting diode_pw2dw_06v0 diode')
extract_devices(diode('diode_pw2dw_06v0'), { 'N' => dnwell, 'P' => diode_pw2dw_06v0_terminal_p })

# diode_dw2ps_03v3 diode
logger.info('Extracting diode_dw2ps_03v3 diode')
extract_devices(diode('diode_dw2ps_03v3'), { 'N' => dnwell, 'P' => diode_dw2ps_03v3_terminal_p })

# diode_dw2ps_06v0 diode
logger.info('Extracting diode_dw2ps_06v0 diode')
extract_devices(diode('diode_dw2ps_06v0'), { 'N' => dnwell, 'P' => diode_dw2ps_06v0_terminal_p })

# sc_diode diode
logger.info('Extracting sc_diode diode')
extract_devices(diode('sc_diode'), { 'N' => sc_diode_terminal_n, 'P' => schottky_diode })
"""

DERIVATIONS = """diode_nd2ps_03v3_terminal_n = ncomp_lv.not(dnwell).and(diode_mk)
                                      .not(diode_nd2ps_exclude)

""" + r"""# sc_diode diode
sc_diode_exclude = dualgate.join(pplus).join(diode_mk)
                           .join(esd_mk).join(lvs_source).join(diode_exclude)

sc_diode_terminal_n = ncomp.and(dnwell).and(schottky_diode).not(sc_diode_exclude)
sc_diode_terminal_p = metal1_con.and(dnwell).not_interacting(sc_diode_terminal_n).not(sc_diode_exclude)
"""

CONNECTIONS = r"""# diode_pd2nw_06v0 diode
connect(diode_pd2nw_06v0_terminal_p, contact)
connect(diode_pd2nw_06v0_terminal_n, ntap)

# diode_pd2nw_06v0_dn diode
connect(diode_pd2nw_06v0_dn_terminal_p, contact)
connect(diode_pd2nw_06v0_dn_terminal_n, ntap)

# diode_nw2ps_03v3 diode
connect(diode_nw2ps_03v3_terminal_p, contact)
connect(diode_nw2ps_03v3_terminal_n, nwell_con)

# diode_nw2ps_06v0 diode
connect(diode_nw2ps_06v0_terminal_p, contact)
connect(diode_nw2ps_06v0_terminal_n, nwell_con)

# diode_pw2dw_03v3 diode
connect(diode_pw2dw_03v3_terminal_p, contact)

# diode_pw2dw_06v0 diode
connect(diode_pw2dw_06v0_terminal_p, contact)

# diode_dw2ps_03v3 diode
connect(diode_dw2ps_03v3_terminal_p, ptap)

# diode_dw2ps_06v0 diode
connect(diode_dw2ps_06v0_terminal_p, ptap)

# sc_diode diode
connect(sc_diode_terminal_n, contact)
connect(sc_diode_terminal_p, schottky_diode)
"""

JUNCTIONS = [f"diode_{k}_{v}" for k in ("nd2ps", "pd2nw") for v in ("03v3", "03v3_dn", "06v0", "06v0_dn")]


def _root(tmp_path, extraction=EXTRACTION, derivations=DERIVATIONS, connections=CONNECTIONS):
    root = tmp_path / "gf180mcuD"
    decks = root / mod.DECKS
    decks.mkdir(parents=True)
    (root / mod.EXTRACTION).write_text(extraction)
    (root / mod.DERIVATIONS).write_text(derivations)
    (root / mod.CONNECTIONS).write_text(connections)
    return root


def _calls(text):
    """-> [(device, N input, P input)] of every extract_devices call."""
    flat = re.sub(r"\s+", " ", text)
    return re.findall(r"extract_devices\(diode\('(\w+)'\), \{ 'N' => (\w+), 'P' => (\w+) \}\)", flat)


def test_every_junction_diode_takes_its_well_side_from_its_own_footprint(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "14"]) == 0
    calls = _calls((root / mod.EXTRACTION).read_text())
    assert len(calls) == len(_calls(EXTRACTION)) == 19
    junction = [c for c in calls if re.match(r"diode_(nd2ps|pd2nw)_", c[0])]
    assert len(junction) == 12
    for dev, n, p in junction:
        if dev.startswith("diode_nd2ps_"):
            # the N side (the diffusion) is untouched; P is that diffusion on lvpwell
            assert p == n + "_pwell", (dev, n, p)
        else:
            assert n == p + "_nwell", (dev, n, p)
    # no junction diode still reads a whole well
    assert not any(p == "lvpwell_con" or n.endswith("_terminal_n") and "pd2nw" in n for _, n, p in junction)


def test_the_other_diodes_are_untouched(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "14"]) == 0
    before = [c for c in _calls(EXTRACTION) if not re.match(r"diode_(nd2ps|pd2nw)_", c[0])]
    after = [c for c in _calls((root / mod.EXTRACTION).read_text()) if not re.match(r"diode_(nd2ps|pd2nw)_", c[0])]
    assert before == after and len(before) == 7  # nw2ps x2, pw2dw x2, dw2ps x2, sc_diode


def test_each_footprint_is_the_junction_on_its_well_and_is_connected_to_it(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "14"]) == 0
    dv = (root / mod.DERIVATIONS).read_text()
    cn = (root / mod.CONNECTIONS).read_text()
    # appended: every line the volume shipped is still there, in order
    assert dv.startswith(DERIVATIONS) and cn.startswith(CONNECTIONS)
    for v in ("03v3", "03v3_dn", "06v0", "06v0_dn"):
        # the subject of the `and` is the diode's own diffusion, never the well
        assert f"diode_nd2ps_{v}_terminal_n_pwell = diode_nd2ps_{v}_terminal_n.and(lvpwell_con)\n" in dv
        assert (f"diode_pd2nw_{v}_terminal_p_nwell = "
                f"diode_pd2nw_{v}_terminal_p.and(diode_pd2nw_{v}_terminal_n)\n") in dv
        assert f"connect(diode_nd2ps_{v}_terminal_n_pwell, lvpwell_con)\n" in cn
        assert f"connect(diode_pd2nw_{v}_terminal_p_nwell, nwell_con)\n" in cn
    # the old well terminals keep their connections
    assert "connect(diode_pd2nw_06v0_terminal_n, ntap)\n" in cn


def test_a_second_application_is_refused(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "14"]) == 0
    snap = {p: (root / p).read_text() for p in (mod.EXTRACTION, mod.DERIVATIONS, mod.CONNECTIONS)}
    assert mod.main(["--root", str(root), "--expect", "14"]) == 2
    assert snap == {p: (root / p).read_text() for p in snap}


def test_a_moved_deck_is_refused_and_nothing_is_written(tmp_path):
    # upstream 417ae8e9 merged the derivations into diode_extraction.lvs; any
    # call that is not the exact shipped text must stop the build.
    moved = EXTRACTION.replace(
        "extract_devices(diode('diode_nd2ps_06v0'), { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell_con })",
        "extract_devices(diode('diode_nd2ps_06v0'), { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell })")
    assert moved != EXTRACTION
    root = _root(tmp_path, extraction=moved)
    assert mod.main(["--root", str(root), "--expect", "14"]) == 2
    assert (root / mod.EXTRACTION).read_text() == moved
    assert (root / mod.DERIVATIONS).read_text() == DERIVATIONS
    assert (root / mod.CONNECTIONS).read_text() == CONNECTIONS


def test_a_missing_file_is_refused(tmp_path):
    root = _root(tmp_path)
    (root / mod.CONNECTIONS).unlink()
    assert mod.main(["--root", str(root), "--expect", "14"]) == 2
    assert (root / mod.EXTRACTION).read_text() == EXTRACTION


def test_check_writes_nothing_and_a_wrong_expectation_fails(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--check"]) == 1
    assert mod.main(["--root", str(root), "--expect", "13"]) == 2
    assert (root / mod.EXTRACTION).read_text() == EXTRACTION
    assert (root / mod.DERIVATIONS).read_text() == DERIVATIONS


def test_the_step_is_declared_and_wired():
    manifest = json.loads((HERE.parent / "tools" / "pdk" / "local_mods.json").read_text())
    entry = [m for m in manifest["modifications"]
             if m["id"] == "gf180mcu-lvs-diode-junctions-extracted-in-their-cell"]
    assert len(entry) == 1
    entry = entry[0]
    base = "gf180mcu/versions/*/gf180mcuD/libs.tech/klayout/tech/lvs/rule_decks/"
    assert entry["paths"] == [base + f for f in
                              ("diode_derivations.lvs", "diode_extraction.lvs", "diode_connections.lvs")]
    assert (entry["expect_modified"], entry["expect_added"], entry["expect_removed"]) == (3, 0, 0)
    assert entry["step"].endswith("pdk_gf180_lvs_diode_hier_patch.py --root /foss/pdks/gf180mcuD --expect 14")
    assert entry["fork_commits"] == [] and entry["fork_note"]
    dockerfile = (HERE.parent / "Dockerfile").read_text()
    assert dockerfile.count("pdk_gf180_lvs_diode_hier_patch.py") == 1
    at = dockerfile.index("pdk_gf180_lvs_diode_hier_patch.py")
    # In the gf180 RUN, after the LAYERS_RC patch, before that RUN's pycache sweep.
    assert dockerfile.index("pdk_gf180_layers_rc_units_patch.py") < at
    run_end = dockerfile.index("-name __pycache__", at)
    assert "\nRUN " not in dockerfile[at:run_end]
    assert "--expect 14" in dockerfile[at:run_end]
    assert "grep -q \"'P' => lvpwell_con\"" in dockerfile[at:run_end]
