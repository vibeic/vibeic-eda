"""A pin the build ENFORCES is integration, clone or no clone. vibeic-eda#60.

`parse_dockerfile_pins` recorded a pin only where it could see a
`github.com/vibeic/<tool>` URL, because a pin had always meant "the build clones
this at this ref". That is not the only way a fork determines what ships.

`open_pdks` is the case. The image's sky130A and gf180mcuD are prebuilt PDK
volumes ciel materialises, and /foss/pdks/sky130A is a symlink into
`.../versions/<open_pdks-sha>/` — so the PDK version IS an open_pdks commit. The
composing Dockerfile declares it and ASSERTS at build time that the shipped
symlink carries it. There is no clone, so the URL-driven loop saw nothing and the
ledger read `integrated=false` about the fork that decides what every DRC and LVS
run reads.

MEASURED after the change, over the composing Dockerfile plus every tool one:

    sv2v          PINNED 6662fa5da71f   via clone
    ciel          PINNED 714d1bbb626d   via clone
    IHP-Open-PDK  PINNED 22f2a25f1734   via clone
    open_pdks     PINNED b344c97eacc2   OPEN_PDKS_VOLUME_CONTENTS_SHA,
                                        asserted at build time

AND WHAT IT MUST NOT DO. A bare `ARG` nobody checks is a comment with a colour;
counting it as integration would be the unverified-pin defect this issue is
about, one level up.

vibeic-eda#79 SPLIT THAT LAST ROW OFF, and the tests below follow it. Being
ENFORCED without a clone makes a value integration; it does not make it a PIN.
`_REF` means build input — sweep it. `_VOLUME_CONTENTS_SHA` means claim about a
prebuilt artefact — do not. The row now carries `pin_kind` so no reader has to
infer that from a comment, which is what #74 and #78 each failed to do.
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


# ── vibeic-eda#79 — and it is a claim, not a pin ──────────────────────────
def test_the_real_tree_calls_open_pdks_an_assertion_not_a_pin():
    """THE #79 REGRESSION, on the real Dockerfile.

    Two PRs proposed advancing this value and the build guard refused both. It
    is not a build input: nothing clones open_pdks, the PDK is a prebuilt ciel
    volume, and the ARG records which upstream commit that volume carries.
    Measured on the published 0.2.63 image:

        /foss/pdks/sky130A -> ciel/sky130/versions/b344c97e…/sky130A
        /foss/pdks/sky130A/SOURCES: `open_pdks b344c97e…`

    The row must SAY that, because every reader that got it wrong had only a
    comment to go on."""
    root = _HERE.parent
    pins = {}
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        pins.update(_pins(f.read_text(encoding="utf-8")))
    op = pins["open_pdks"]
    assert op["pin_kind"] == "contents_assertion"
    assert op["arg"] == "OPEN_PDKS_VOLUME_CONTENTS_SHA"
    assert "not a build input" in op["pinned_via"]


def test_no_pin_named_REF_is_reclassified_by_this_pass():
    """The negative control. #79 must not turn any existing pin into an
    assertion — the change is confined to the new suffix."""
    root = _HERE.parent
    pins = {}
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        pins.update(_pins(f.read_text(encoding="utf-8")))
    wrong = {t: p for t, p in pins.items()
             if p.get("pin_kind") == "contents_assertion"
             and str(p.get("arg", "")).endswith("_REF")}
    assert not wrong, f"a `_REF` pin was reclassified as an assertion: {wrong}"


def test_an_enforced_contents_assertion_is_recorded_as_one():
    t = (f"ARG VOL_VOLUME_CONTENTS_SHA={_SHA}\nFROM ubuntu:24.04\n"
         "ARG VOL_VOLUME_CONTENTS_SHA\n"
         'RUN test -n "${VOL_VOLUME_CONTENTS_SHA}" && '
         'readlink -f /x | grep -q "${VOL_VOLUME_CONTENTS_SHA}"\n')
    v = _pins(t)["vol"]
    assert v["ref"] == _SHA and v["pin_kind"] == "contents_assertion"


def test_an_assertion_name_on_something_the_build_FETCHES_stays_a_pin():
    """LOAD-BEARING, and the direction that is easy to leave open. A convention
    nobody corroborates is an escape hatch: name a real pin
    `*_VOLUME_CONTENTS_SHA` and every sweep stops looking at it, which is #60's
    unverified pin wearing the opposite mask. The file's own text decides."""
    t = (f"ARG SNEAK_REPO=https://github.com/vibeic/sneak.git\n"
         f"ARG SNEAK_VOLUME_CONTENTS_SHA={_SHA}\nFROM ubuntu:24.04\n"
         "ARG SNEAK_VOLUME_CONTENTS_SHA\n"
         'RUN git clone "${SNEAK_REPO}" /s && cd /s && '
         "git checkout ${SNEAK_VOLUME_CONTENTS_SHA}\n")
    import pin_kinds
    k = pin_kinds.classify(t)["SNEAK_VOLUME_CONTENTS_SHA"]
    assert k["kind"] == "pin" and k["misnamed"] is True
    assert pin_kinds.contents_assertions(t) == {}, \
        "a fetched ref must never be exempted from the sweep by its name"
    assert pin_kinds.misnamed_pins(t), "and it must be reported, not just fixed"


def test_a_bare_assertion_nobody_asserts_is_not_honoured():
    """Same rule as the ARG-only pin above, applied to the new suffix: a
    declaration nothing checks is a comment with a colour."""
    import pin_kinds
    t = f"ARG DECOR_VOLUME_CONTENTS_SHA={_SHA}\nFROM ubuntu:24.04\nRUN echo hi\n"
    assert pin_kinds.classify(t)["DECOR_VOLUME_CONTENTS_SHA"]["kind"] == "unenforced"
    assert pin_kinds.contents_assertions(t) == {}
    assert "decor" not in _pins(t)


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
