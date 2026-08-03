"""A pin the build ENFORCES is integration, clone or no clone. vibeic-eda#60.

`parse_dockerfile_pins` recorded a pin only where it could see a
`github.com/vibeic/<tool>` URL, because a pin had always meant "the build clones
this at this ref". That is not the only way a fork determines what ships.

`open_pdks` is the case. The image's sky130A and gf180mcuD are prebuilt PDK
volumes ciel materialises, and /foss/pdks/sky130A is a symlink into
`.../versions/<open_pdks-sha>/` — so the PDK version IS an open_pdks commit. The
composing Dockerfile now declares `ARG OPEN_PDKS_REF` and ASSERTS at build time
that the shipped symlink carries it. There is no clone, so the URL-driven loop
saw nothing and the ledger read `integrated=false` about the fork that decides
what every DRC and LVS run reads.

MEASURED after the change, over the composing Dockerfile plus every tool one:

    sv2v          PINNED 6662fa5da71f   via clone
    ciel          PINNED 714d1bbb626d   via clone
    IHP-Open-PDK  PINNED 22f2a25f1734   via clone
    open_pdks     PINNED b344c97eacc2   OPEN_PDKS_REF, asserted at build time

AND WHAT IT MUST NOT DO. A bare `ARG` nobody checks is a comment with a colour;
counting it as integration would be the unverified-pin defect this issue is
about, one level up.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "discover_forks", _HERE / "discover_forks.py")
D = importlib.util.module_from_spec(_spec)
sys.modules["discover_forks"] = D
try:
    _spec.loader.exec_module(D)
except SystemExit:
    pass

_SHA = "b" * 40


def _pins(text):
    return D.parse_dockerfile_pins(text)


# ── what it must record ───────────────────────────────────────────────────
def test_an_enforced_arg_only_pin_is_recorded():
    t = (f"ARG ENFORCED_REF={_SHA}\nFROM ubuntu:24.04\nARG ENFORCED_REF\n"
         'RUN test -n "${ENFORCED_REF}" && grep -q "${ENFORCED_REF}" /x\n')
    p = _pins(t)
    assert "enforced" in p
    assert p["enforced"]["ref"] == _SHA
    assert "asserted at build time" in p["enforced"]["pinned_via"]


def test_it_says_HOW_rather_than_looking_like_a_clone():
    """A row that reads the same for a cloned fork and a prebuilt artefact hides
    an operational difference: one moves when the ref moves, the other moves
    only when someone re-cuts the artefact."""
    t = (f"ARG ENFORCED_REF={_SHA}\nFROM ubuntu:24.04\nARG ENFORCED_REF\n"
         'RUN grep -q "${ENFORCED_REF}" /x\n')
    e = _pins(t)["enforced"]
    assert e["submodules"] is False and e["recursive"] is False
    assert "no clone" in e["pinned_via"]


def test_the_real_tree_records_all_four():
    """The regression. Three by clone, one by enforced ARG — and #60 is closed
    only if the ledger can see all four."""
    root = _HERE.parent
    pins = {}
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        pins.update(_pins(f.read_text(encoding="utf-8")))
    for tool in ("sv2v", "ciel", "ihp-open-pdk", "open_pdks"):
        assert tool in pins, f"{tool} is not seen as pinned"
    assert "asserted at build time" in pins["open_pdks"]["pinned_via"]


# ── what it must NOT record ───────────────────────────────────────────────
def test_a_bare_ARG_nobody_checks_is_not_integration():
    """LOAD-BEARING. Counting an unverified declaration as integration is the
    defect #60 is about, moved up one level: the number would be declared and
    the image could still carry something else."""
    t = f"ARG DECOR_REF={_SHA}\nFROM ubuntu:24.04\nRUN echo hi\n"
    assert "decor" not in _pins(t)


def test_a_branch_name_is_not_a_commit():
    """`A PIN IS A COMMIT` — the rule this parser already states. A ref that is
    a branch tracks whatever that branch becomes, which is the opposite of a
    pin."""
    t = ("ARG LOOSE_REF=main\nFROM ubuntu:24.04\nARG LOOSE_REF\n"
         "RUN echo ${LOOSE_REF}\n")
    assert "loose" not in _pins(t)


def test_a_clone_driven_pin_is_unchanged():
    """The pass must ADD, never reinterpret: a tool the URL loop already found
    keeps exactly the row it had."""
    t = (f"ARG YOSYS_REPO=https://github.com/vibeic/yosys.git\n"
         f"ARG YOSYS_REF={_SHA}\nFROM ubuntu:24.04\n"
         'RUN git clone "${YOSYS_REPO}" /y && cd /y && git checkout ${YOSYS_REF}\n')
    y = _pins(t)["yosys"]
    assert y["ref"] == _SHA
    assert "pinned_via" not in y, "a cloned tool must not gain the ARG-only note"
