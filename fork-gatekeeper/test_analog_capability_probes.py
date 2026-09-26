"""The three analog capability probes judge VALUES, and each can turn red.

Samples are the probe output measured on two real images (2026-09-26):
0.3.79 (sha256:93d88e9e...), where all three capabilities are absent, and an
image composed with `bake eda` from this branch (local tag gkfix240, image id
sha256:b13a13640408...; CACE_REF 0479f0e, eda-tool-iverilog:0745426-00ec8d),
where all three work. The *_OK samples are that image's subject output
VERBATIM, as capability_smoke's runner captures it (noise lines filtered).

They replaced hand-edited samples no probe had printed: CACE_OK carried
Vd tt/ss/ff = 0.619812/0.823819/0.393632 from an earlier deck (the probe's deck,
rd 20k with vg 0.6 V, gives 0.792873/0.971208/0.559596, measured both on the
PR-240 candidate and on this 0479f0e image); XYCE_OK carried MOS_REL=1.1e-08, which the probe's own
`"MOS_REL=%.3e"` cannot print; COSIM_OK carried 1.200000e+00 where ngspice
prints 1.20000e+00. A verdict test on a sample the probe cannot produce tests
the verdict function against a world that does not exist.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capability_smoke as S                                     # noqa: E402

# -- Xyce vs ngspice ---------------------------------------------------------
XYCE_OK = ("XRC=0\nRC=0\nMOS_REL=0.000e+00\nNB_REL=2.378e-03\n"
           "RES_REL=7.805e-03\nMULT4=4.000000")
XYCE_079 = "XRC=1\nRC=0\nNetlist error: Failed to load plugin\n"


def test_xyce_agreement_passes_on_the_candidate_values():
    assert S._rc(XYCE_OK, "XRC") == 0 and S._xyce_agrees(XYCE_OK) is None


def test_xyce_without_plugins_is_red_on_the_exit_code():
    assert S._rc(XYCE_079, "XRC") == 1


def test_a_wrong_mos_current_is_red():
    bad = XYCE_OK.replace("MOS_REL=0.000e+00", "MOS_REL=2.000e-02")
    assert "MOS Id differs" in S._xyce_agrees(bad)


def test_an_ignored_mult_is_red():
    # a plugin that dropped m= would give m=4 the m=1 current
    bad = XYCE_OK.replace("MULT4=4.000000", "MULT4=1.000000")
    assert "m=4" in S._xyce_agrees(bad)


def test_a_diode_node_off_by_more_than_one_percent_is_red():
    bad = XYCE_OK.replace("NB_REL=2.378e-03", "NB_REL=1.500e-02")
    assert "diode-connected" in S._xyce_agrees(bad)


def test_missing_comparison_is_red_not_green():
    assert "no Xyce/ngspice comparison" in S._xyce_agrees("XRC=0\nRC=0\n")


# -- CACE .spice template ----------------------------------------------------
CACE_OK = "RC=0\nVD_tt=0.792873\nVD_ss=0.971208\nVD_ff=0.559596"
CACE_079 = ("RC=0\n[06:21:33] ERROR    No netlist or schematic for project"
            "   cace_regenerate.py:585\nVD_tt=\nVD_ss=\nVD_ff=\n")


def test_cace_orders_the_corners():
    assert S._cace_orders(CACE_OK) is None


def test_cace_refusal_is_red():
    assert "refused" in S._cace_orders(CACE_079)


def test_an_unsubstituted_corner_is_red():
    # a literal template simulates one corner three times
    same = "RC=0\nVD_tt=0.792873\nVD_ss=0.792873\nVD_ff=0.792873"
    assert "corner not substituted" in S._cace_orders(same)


# -- ngspice d_cosim ---------------------------------------------------------
COSIM_OK = "RC=0\nVLOG_COSIM_ALIVE\nvhigh               =  1.20000e+00"
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
    bad = COSIM_OK.replace("1.20000e+00", "0.00000e+00")
    assert "dac_bridge" in S._cosim_ok(bad)


def test_the_three_probes_are_registered():
    names = {p.name for p in S.PROBES}
    assert {"xyce/ihp-plugins-vs-ngspice", "cace/spice-template",
            "ngspice/d-cosim-icarus"} <= names


def test_the_xyce_sample_is_printable_by_the_probe():
    """Every KEY=value in XYCE_OK must be what the probe's own print() emits.

    The formats are read from the probe script, not restated here, so a probe
    that changes its format and a sample that was typed by hand both turn this
    red.
    """
    probe = next(p for p in S.PROBES if p.name == "xyce/ihp-plugins-vs-ngspice")
    fmts = dict(re.findall(r'print\("([A-Z0-9_]+)=(%[-+ #0-9.]*[eEfg])"', probe.script))
    assert set(fmts) == {"MOS_REL", "NB_REL", "RES_REL", "MULT4"}, fmts
    for key, value in re.findall(r"^([A-Z0-9_]+_REL|MULT4)=(\S+)$", XYCE_OK, re.M):
        assert fmts[key] % float(value) == value, (
            f"{key}={value} is not something `{fmts[key]}` prints")
