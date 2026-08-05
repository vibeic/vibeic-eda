"""vibeic-eda#81 — two consumers of one ledger published different gap counts.

MEASURED 2026-08-04, both sides on `origin/main` at 01e06c5, same ledger, same
minute:

    fork_gap_report.py   Q1  image behind upstream : 21 across 6 fork(s)
                             CONTENTS ASSERTIONS (not pins, no gap to close):
                             open_pdks=b344c97eacc2
    the published page   Commits behind upstream   : 39 (7 fork(s))

The whole difference was `open_pdks`' 18 — 86% of the page's headline — and that
number had already been cited in #74 and #78 as the reason to advance
`OPEN_PDKS_VOLUME_CONTENTS_SHA`, which the build guard refused both times because
nothing fetches at it.

WHICH SIDE WAS WRONG, and it is not a tie. `fork_gap_report` asks `pin_kinds`
what the ARG MEANS. `discover_forks` asks the same module and writes the answer
onto every ledger row as `pin_kind`. `gatekeeper` reads that field to decide
candidacy. The page read NOTHING: its population of "rows that are not a gap" was
empty while the real one had a member, so it spoke for a classification it could
not see and reported every row as closable.

THE FIX IS A FIELD, NOT A LIST. The page now partitions on the ledger's own
`pin_kind`. An exclusion list of tool names would pass the "stop counting
open_pdks" half and fail both halves that matter: a genuine build-input pin that
is behind must still count, and an ARG that merely LOOKS like an assertion —
`*_VOLUME_CONTENTS_SHA` on something a fetch step reads — is a misnamed PIN,
comes back from `pin_kinds` as `pin`, and must keep being counted. Both
directions are asserted below, in JS against the shipped expressions and in
Python against the real tree.
"""
from __future__ import annotations

import importlib.util
import json
import pytest
import re
import pathlib
import shutil
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _node():
    return shutil.which("node")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


# ── the shipped JS rule ───────────────────────────────────────────────────────
def test_the_shipped_gap_rule_partitions_on_pin_kind(capsys):
    """Eight cases against the expressions the page actually ships.

    WHY NODE. The rule is JavaScript inside `build_page.py`; a Python
    re-implementation would test the re-implementation. `jstests/gap_kind.mjs`
    slices the shipped block out and evaluates it — and its slice boundaries are
    the ones that existed BEFORE this fix, so pointing it at the pre-fix file
    reproduces `[39, 7]` rather than failing to find itself.
    """
    node = _node()
    if not node:
        return
    r = subprocess.run([node, str(_HERE / "jstests" / "gap_kind.mjs"),
                        str(_HERE / "build_page.py")],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_page_names_no_tool_in_its_gap_rule():
    """LOAD-BEARING, and the half an exclusion list would fail. The rule may not
    know any tool by name: the next prebuilt artefact has to be excluded on the
    morning `discover_forks` first classifies it, with no edit here."""
    src = (_HERE / "build_page.py").read_text(encoding="utf-8")
    seg = src[src.index("// THE TWO NUMBERS THIS PAGE EXISTS FOR."):
              src.index("const patchForks")]
    # The declarations only. The prose above them cites the tool that produced
    # the defect, which is documentation and must stay.
    code = "\n".join(l for l in seg.splitlines()
                     if l.strip().startswith("const "))
    for tool in ("open_pdks", "OPEN_PDKS", "ciel", "sky130", "IHP"):
        assert tool not in code, (
            f"the gap rule names `{tool}`; an exclusion by name is a list "
            f"someone must maintain, which is the fix #81 forbids")
    assert "pin_kind" in code, "the rule must key on the ledger's recorded kind"


def test_the_page_does_not_claim_to_list_rows_it_no_longer_renders():
    """The contents-assertion LIST was removed on owner instruction 2026-08-05.

    The principle behind the test this replaces still holds and is what is
    asserted here: a row that vanishes is a row nobody can audit, so the page
    must not trade a wrong number for a hidden one. Two things keep that true
    after the removal:

    * the excluded rows are still COUNTED and still NAMED in the summary
      ("N contents assertion(s) excluded"), so the reader knows they exist;
    * `fork_gap_report` still prints them under their own heading, which is the
      auditable record. The page is the summary, not the ledger.

    What would be dishonest is the page saying the rows are "listed below" when
    nothing lists them. That sentence shipped for one build after the block was
    removed, and this is the assertion that catches it returning.
    """
    src = (_HERE / "build_page.py").read_text(encoding="utf-8")

    assert "listed below" not in src, (
        "the summary still points the reader at a list of contents assertions, "
        "but the block that rendered them was removed — the page is describing "
        "something it does not show")
    assert "列在下方" not in src, (
        "the Chinese summary still says the rows are listed below, and they "
        "are not")

    # The KPI label lost its count when the labels were shortened on owner
    # instruction. That is acceptable ONLY because the rows themselves survive:
    # every contents-assertion fork still gets its own row in the main table,
    # driven from the same LEDGERS array as every other fork. Measured rather
    # than assumed -- `open_pdks` is the fleet's only such row today and it is
    # present in the emitted page's LEDGERS.
    #
    # So the surviving principle is unchanged: a row that VANISHES is a row
    # nobody can audit. A row that is merely not summarised in a headline is
    # still auditable, and that is what this now asserts.
    page = _HERE.parent.parent / "vibeic.ai" / "eda-forks.html"
    if not page.is_file():
        pytest.skip("no published page to inspect")
    html = page.read_text(errors="replace")
    m = re.search(r"const LEDGERS = (\[.*?\]);\n", html, re.S)
    assert m, "the page carries no LEDGERS array at all"
    rows = json.loads(m.group(1))
    names = {r.get("tool") or r.get("repo") for r in rows}
    kinds = {(r.get("tool") or r.get("repo")): r.get("pin_kind") for r in rows}
    assertion_rows = [n for n, k in kinds.items() if k == "contents_assertion"]
    if not assertion_rows:
        pytest.skip("no contents-assertion fork on the fleet right now")
    for n in assertion_rows:
        assert n in names, (
            f"{n} is a contents assertion and has no row in the published page; "
            f"removing the summary block must not remove the row")


def test_the_empty_gap_container_is_hidden_not_drawn():
    """`.fork-gap` carries a 3px orange left border and a tinted background, so
    an empty one renders as a bare coloured line with nothing in it. Once the
    tracking gap reaches zero — the goal state — that line is all that is left,
    and it reads as a defect marker for the absence of defects."""
    src = (_HERE / "build_page.py").read_text(encoding="utf-8")
    assert "gapEl.hidden = !gapEl.innerHTML.trim()" in src, (
        "the gap container is not hidden when empty; at zero gaps the page "
        "draws a styled, empty box")


# ── the real tree: one authority, one field, one population ──────────────────
def test_report_and_ledger_exclude_the_same_tools_on_the_real_tree():
    """THE INVARIANT #81 IS ABOUT. The report classifies from the Dockerfile; the
    page classifies from the ledger field. Both answers come from `pin_kinds`, so
    the two populations must be identical — if they are not, the page and the
    report can disagree again by a different route."""
    fgr = _load("fork_gap_report")
    disc = _load("discover_forks")
    root = _HERE.parent

    # what the REPORT excludes: stems `pin_kinds` calls enforced assertions
    report_stems = set()
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        import pin_kinds
        report_stems |= set(pin_kinds.contents_assertions(
            f.read_text(encoding="utf-8")))
    assert report_stems, "no assertion in the tree — this test would prove nothing"

    # what the LEDGER records, and therefore what the PAGE excludes
    pins = {}
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        pins.update(disc.parse_dockerfile_pins(f.read_text(encoding="utf-8")))
    ledger_tools = {t for t, p in pins.items()
                    if p.get("pin_kind") == "contents_assertion"}

    assert {s.lower() for s in report_stems} == ledger_tools, (
        f"the report excludes {sorted(report_stems)} but the ledger marks "
        f"{sorted(ledger_tools)}; the page keys on the ledger, so the two "
        f"consumers would publish different totals again")

    # and the report's own tool-name lookup reaches every one of them
    for stem in report_stems:
        assert fgr.pin_kinds.is_assertion_arg(stem + "_VOLUME_CONTENTS_SHA")


def test_a_dashed_repository_name_still_reaches_its_assertion():
    """The derivation must be GENERAL, not lucky. `open_pdks` matches only
    because its repository name has no dash: the ARG-only loop keys on the ARG
    STEM (`IHP_OPEN_PDK`) while the ledger row is named for the REPOSITORY
    (`IHP-Open-PDK`), and an exact lookup misses. The row would then read
    `integrated=false, pin_kind=null`, the page would count its commits as a gap,
    and `fork_gap_report` — already separator-insensitive — would not. #81, one
    artefact later."""
    disc = _load("discover_forks")
    sha = "c" * 40
    text = (f"ARG IHP_OPEN_PDK_VOLUME_CONTENTS_SHA={sha}\nFROM ubuntu:24.04\n"
            "ARG IHP_OPEN_PDK_VOLUME_CONTENTS_SHA\n"
            'RUN readlink -f /x | grep -q "${IHP_OPEN_PDK_VOLUME_CONTENTS_SHA}"\n')
    pins = disc.parse_dockerfile_pins(text)
    assert "ihp-open-pdk" not in pins, (
        "fixture no longer reproduces the spelling mismatch it was written for")
    got = disc._pin_for_tool(pins, "IHP-Open-PDK")
    assert got.get("pin_kind") == "contents_assertion", got
    assert disc._pin_for_tool(pins, "ihp_open_pdk").get("ref") == sha


def test_the_separator_fallback_invents_no_pin_on_the_real_tree():
    """The negative control for the lookup above. A looser match that hands a
    tool someone else's pin is worse than a miss — the row reads as tracked. On
    the real tree the exact branch must answer for every tool that resolves
    today, and the fallback must add nothing."""
    disc = _load("discover_forks")
    root = _HERE.parent
    pins = {}
    for f in [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")):
        pins.update(disc.parse_dockerfile_pins(f.read_text(encoding="utf-8")))
    names = [f["tool"] for f in json.loads(
        (_HERE / "FORKS.json").read_text())["forks"]]
    assert len(names) > 30, f"only {len(names)} tools — the sweep proves little"

    def flat(s):
        return s.lower().replace("-", "").replace("_", "")

    gained = []
    for name in names:
        exact = pins.get(name.lower())
        got = disc._pin_for_tool(pins, name)
        if exact is not None:
            assert got is exact, f"{name}: the fallback overrode an exact match"
            continue
        if got:
            gained.append(name)
            # a name with no exact key may only gain a pin whose OWN key
            # normalises to the same string — never an unrelated one
            assert any(flat(k) == flat(name) for k, v in pins.items() if v is got), \
                f"{name} was handed an unrelated pin: {got}"
    assert not gained, (
        f"the fallback changed this tree's answer for {gained}; it was added to "
        f"make the derivation general, not to move a live row")
