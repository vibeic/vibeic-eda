"""The gf180mcu Xyce tree defines the two device names the 5 V cells call.

The volume's ngspice tree defines `nfet_05v0` / `pfet_05v0` as wrappers of
the 6 V model; its Xyce tree does not, and Xyce 7.10 refuses every 5 V
standard cell: "Subcircuit NFET_05V0 has not been defined for instance X_I_1".
The port copies the Xyce tree's OWN 6 V wrapper under the 5 V name with the
ngspice definition's defaults, and refuses whatever it cannot prove is the
same wrapper.

These tests pin the TRANSFORM on the same shapes in miniature; they need no PDK.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "tools" / "pdk" / "pdk_gf180_xyce_05v0_patch.py"
_spec = importlib.util.spec_from_file_location("pdk_gf180_xyce_05v0_patch", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

HEAD = "nf=1 m=1 as=0 ad=0 ps=0 pd=0 nrd=0 nrs=0 par=1 dtemp=0 sa=0 sb=0 sd=0"


def _wrapper(name, model, w, l, vth, k, pl, pw, dialect, quoted_mis=True):
    q = (lambda v: f"'{v}'") if quoted_mis else (lambda v: v)
    device = (f"m0 d g s b {model} ad=ad as=as delvto='mis_vth*sw_stat_mismatch' l=l m=m "
              f"mulu0='1-mis_k*sw_stat_mismatch' nf=nf nrd=nrd nrs=nrs pd=pd ps=ps sa=sa "
              f"sb=sb sd=sd w=w" if dialect == "ngspice" else
              f"m0 d g s b {model} ad='ad' as='as' delvto='mis_vth*sw_stat_mismatch' l='l' "
              f"m='m' nrd='nrd' nrs='nrs' pd='pd' ps='ps' w='w'")
    return (f".subckt {name} d g s b w={w} l={l} {HEAD}\n"
            f".param par_vth={vth}\n.param par_k={k}\n.param par_l={pl}\n.param par_w={pw}\n"
            f".param par_leff='l-par_l'\n"
            f".param mis_k={q('agauss(0,var_k,1)')}\n.param mis_vth={q('agauss(0,var_vth,1)')}\n"
            f"{device}\n.ends {name}\n")


NGSPICE = (".lib typical\n.lib 'sm141064.ngspice' fets_mm\n.endl typical\n\n.lib fets_mm\n"
           + _wrapper("nfet_05v0", "nfet_06v0", "1e-6", "6e-7", 0.01155, "0.0000", "4e-7", "-5e-7",
                      "ngspice", quoted_mis=False)
           + _wrapper("nfet_06v0", "nfet_06v0", "1e-5", "7e-7", 0.01155, "0.0000", "4e-7", "-5e-7",
                      "ngspice")
           + _wrapper("pfet_05v0", "pfet_06v0", "1e-5", "5e-7", 0.01051, "0.00517", "3e-7", "-4e-7",
                      "ngspice", quoted_mis=False)
           + _wrapper("pfet_06v0", "pfet_06v0", "1e-5", "5.5e-7", 0.01051, "0.00517", "3e-7",
                      "-4e-7", "ngspice")
           + ".endl fets_mm\n")

XYCE = (".lib typical\n.lib 'sm141064.spice' fets_mm\n.endl typical\n\n.lib fets_mm\n"
        + _wrapper("nfet_06v0", "nfet_06v0", "1e-5", "7e-7", 0.01155, "0.0000", "4e-7", "-5e-7",
                   "xyce")
        + _wrapper("pfet_06v0", "pfet_06v0", "1e-5", "5e-7", 0.01051, "0.00517", "3e-7", "-4e-7",
                   "xyce")
        + ".endl fets_mm\n")


def _root(tmp_path, ngspice=NGSPICE, xyce=XYCE):
    root = tmp_path / "gf180mcuD"
    for rel, text in ((mod.NGSPICE, ngspice), (mod.XYCE[0], xyce), (mod.XYCE[1], xyce)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_the_5v_names_are_the_xyce_trees_own_6v_wrappers_with_the_5v_defaults(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0
    for rel in mod.XYCE:
        text = (root / rel).read_text()
        blocks = mod.blocks(text)
        for name, model, w, l in (("nfet_05v0", "nfet_06v0", "1e-6", "6e-7"),
                                  ("pfet_05v0", "pfet_06v0", "1e-5", "5e-7")):
            first, last, body = blocks[name]
            assert body[0].startswith(f".subckt {name} d g s b w={w} l={l} {HEAD}")
            # Xyce's own device line and statistics, not ngspice's (no mulu0/nf/sa).
            assert body[1:-1] == blocks[model][2][1:-1]
            assert "mulu0" not in "".join(body)
            assert body[-1] == f".ends {name}\n"
            # Immediately before its model's wrapper, inside the same section.
            assert last + 1 == blocks[model][0]
            lines = text.splitlines()
            assert lines.index(".lib fets_mm") < first and last < lines.index(".endl fets_mm")
        # Nothing that was there moved or changed.
        assert text.replace("".join(blocks["nfet_05v0"][2]), "").replace(
            "".join(blocks["pfet_05v0"][2]), "") == XYCE
    assert (root / mod.XYCE[0]).read_text() == (root / mod.XYCE[1]).read_text()


def test_a_second_application_is_refused_rather_than_defining_the_names_twice(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "4"]) == 0
    before = (root / mod.XYCE[0]).read_text()
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.XYCE[0]).read_text() == before


def test_a_5v_wrapper_with_its_own_statistics_is_not_copied_from_the_6v_one(tmp_path):
    root = _root(tmp_path, ngspice=NGSPICE.replace("par_vth=0.01155\n", "par_vth=0.02\n", 1))
    assert mod.main(["--root", str(root), "--expect", "4"]) == 2
    assert (root / mod.XYCE[0]).read_text() == XYCE


def test_a_5v_name_that_is_not_a_wrapper_of_the_6v_model_is_refused(tmp_path):
    root = _root(tmp_path, ngspice=NGSPICE.replace("m0 d g s b nfet_06v0 ad=ad", "m0 d g s b nfet_03v3 ad=ad", 1))
    assert mod.main(["--root", str(root)]) == 2
    assert (root / mod.XYCE[0]).read_text() == XYCE


def test_check_writes_nothing_and_a_wrong_expectation_fails(tmp_path):
    root = _root(tmp_path)
    assert mod.main(["--root", str(root), "--check"]) == 1
    assert (root / mod.XYCE[0]).read_text() == XYCE
    assert mod.main(["--root", str(root), "--expect", "2"]) == 2
    assert (root / mod.XYCE[0]).read_text() == XYCE
