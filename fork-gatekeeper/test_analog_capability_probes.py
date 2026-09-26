"""The three analog capability probes judge VALUES, and each can turn red.

Samples are the probe output measured on two real images (2026-09-26):
0.3.79 (sha256:93d88e9e...), where all three capabilities are absent, and the
mig112 candidate built from this branch's recipe, where all three work.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capability_smoke as S                                     # noqa: E402

# -- Xyce vs ngspice ---------------------------------------------------------
XYCE_OK = ("XRC=0\nRC=0\nMOS_REL=1.1e-08\nNB_REL=2.378e-03\n"
           "RES_REL=7.805e-03\nMULT4=4.000002\n")
XYCE_079 = "XRC=1\nRC=0\nNetlist error: Failed to load plugin\n"


def test_xyce_agreement_passes_on_the_candidate_values():
    assert S._rc(XYCE_OK, "XRC") == 0 and S._xyce_agrees(XYCE_OK) is None


def test_xyce_without_plugins_is_red_on_the_exit_code():
    assert S._rc(XYCE_079, "XRC") == 1


def test_a_wrong_mos_current_is_red():
    bad = XYCE_OK.replace("MOS_REL=1.1e-08", "MOS_REL=2.0e-02")
    assert "MOS Id differs" in S._xyce_agrees(bad)


def test_an_ignored_mult_is_red():
    # a plugin that dropped m= would give m=4 the m=1 current
    bad = XYCE_OK.replace("MULT4=4.000002", "MULT4=1.000000")
    assert "m=4" in S._xyce_agrees(bad)


def test_a_diode_node_off_by_more_than_one_percent_is_red():
    bad = XYCE_OK.replace("NB_REL=2.378e-03", "NB_REL=1.5e-02")
    assert "diode-connected" in S._xyce_agrees(bad)


def test_missing_comparison_is_red_not_green():
    assert "no Xyce/ngspice comparison" in S._xyce_agrees("XRC=0\nRC=0\n")


# -- CACE .spice template ----------------------------------------------------
CACE_OK = "RC=0\nVD_tt=0.619812\nVD_ss=0.823819\nVD_ff=0.393632\n"
CACE_079 = ("RC=0\n[06:21:33] ERROR    No netlist or schematic for project"
            "   cace_regenerate.py:585\nVD_tt=\nVD_ss=\nVD_ff=\n")


def test_cace_orders_the_corners():
    assert S._cace_orders(CACE_OK) is None


def test_cace_refusal_is_red():
    assert "refused" in S._cace_orders(CACE_079)


def test_an_unsubstituted_corner_is_red():
    # a literal template simulates one corner three times
    same = "RC=0\nVD_tt=0.619812\nVD_ss=0.619812\nVD_ff=0.619812\n"
    assert "corner not substituted" in S._cace_orders(same)


# -- ngspice d_cosim ---------------------------------------------------------
COSIM_OK = "RC=0\nVLOG_COSIM_ALIVE\nvhigh               =  1.200000e+00\n"
COSIM_079 = ("RC=134\nCannot open shared library libvvp: "
             "/foss/tools/ngspice/lib/ngspice/libvvp.so: cannot open shared "
             "object file: No such file or directory\n")


def test_cosim_passes_on_the_candidate():
    assert S._cosim_ok(COSIM_OK) is None


def test_cosim_without_libvvp_is_red():
    assert "rc=134" in S._cosim_ok(COSIM_079)


def test_cosim_without_the_verilog_side_is_red():
    assert "never ran" in S._cosim_ok("RC=0\nvhigh = 1.2\n")


def test_cosim_bridge_value_is_checked():
    bad = COSIM_OK.replace("1.200000e+00", "0.000000e+00")
    assert "dac_bridge" in S._cosim_ok(bad)


def test_the_three_probes_are_registered():
    names = {p.name for p in S.PROBES}
    assert {"xyce/ihp-plugins-vs-ngspice", "cace/spice-template",
            "ngspice/d-cosim-icarus"} <= names
