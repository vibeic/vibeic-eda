#!/usr/bin/env python3
"""The page must state SYNC lag and RELEASE lag separately, because their fixes are opposite.

    SYNC LAG     our fork trails upstream    -> merge upstream in
    RELEASE LAG  the image's pin trails us   -> bump the pin, rebuild

`fork_gap_report` has printed `[sync N · release M]` since its second docstring
paragraph. `build_page` contained ZERO occurrences of either word, and the data was
not reachable from it either: no ledger row carried the split, so the page could not
have rendered it even if someone had tried. It published one combined number.

MEASURED 2026-08-04: slang was `sync 0 · release 1`. Its fork was EXACTLY LEVEL with
upstream and the card said "behind upstream by 1 commit" — which reads as "go merge
upstream", and merging would have changed nothing. The action that closes it is
advancing `SLANG_REF` and rebuilding.

WHY THIS TEST RUNS THE PAGE'S OWN JAVASCRIPT
============================================
vibeic-eda#83 shipped a test for the neighbouring defect that INJECTED `gapRows` —
the very value the code under test derives. It passed against a build_page with the
working line deleted, because the test supplied the answer it was checking. A test
that constructs the intermediate cannot observe whether the intermediate is computed.

So this evaluates `build_page.PAGE`'s real script in node against a minimal DOM, and
reads what the KPI card ACTUALLY RENDERED. Nothing here defines `splitKnown`,
`syncLag` or `releaseLag`; the only input is `LEDGERS`. Delete any of the lines that
do the work and this goes red, which is the property #83's test lacked.

AND IT RUNS ON THE REAL PRODUCTION LEDGER — all 36 tool rows of
`~/.cache/eda-fork-gatekeeper/ledger`, with the split computed by the real
`discover_forks.lag_split` against the real clones. A synthetic two-row fixture
proves the arithmetic and nothing about whether it survives the shapes production
actually contains (mirrors, vendored pins, branch pins, contents assertions,
unpinned forks).

NO TODAY-SPECIFIC NUMBER IS PINNED. Asserting "slang is release-only" would pin a
moment: the next pin bump closes it and the test fails for the good outcome. What is
asserted is the INVARIANT — whenever the real data contains a release-only fork, the
page must say so rather than call it "behind upstream".
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LEDGER = Path(os.environ.get("GK_STATE_DIR")
              or os.path.expanduser("~/.cache/eda-fork-gatekeeper")) / "ledger"
FORKS_ROOT = Path("/home/reyerchu/vibe-ic-forks")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_rows():
    """Every production ledger row, as the page receives them."""
    if not LEDGER.is_dir():
        pytest.skip(f"no production ledger at {LEDGER}")
    rows = []
    for f in sorted(LEDGER.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            rows.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if not rows:
        pytest.skip("production ledger is empty")
    return rows


# ── the DOM the page's script needs, and nothing more ────────────────────────
# Seven ids plus querySelector/querySelectorAll, enumerated from PAGE itself. A
# fuller stub would let the script fail silently on an element this does not model;
# `getElementById` returning a live object for ANY id is deliberate, so a new element
# never turns this test red for a reason that has nothing to do with the split.
_DOM = r"""
const __el = () => new Proxy({innerHTML:"", textContent:"", value:"", style:{},
  classList:{add(){},remove(){},toggle(){},contains(){return false}},
  appendChild(){}, setAttribute(){}, getAttribute(){return null},
  addEventListener(){}, querySelector(){return __el()}, querySelectorAll(){return []},
  children:[], dataset:{}}, {get(t,k){ return k in t ? t[k] : undefined; },
                             set(t,k,v){ t[k]=v; return true; }});
const __store = {};
globalThis.document = {
  getElementById(id){ return __store[id] || (__store[id] = __el()); },
  querySelector(){ return __el(); },
  querySelectorAll(){ return []; },
  addEventListener(){}, documentElement: __el(), body: __el(),
  createElement(){ return __el(); },
};
globalThis.window = globalThis;
globalThis.localStorage = {getItem(){return null}, setItem(){}, removeItem(){}};
globalThis.matchMedia = () => ({matches:false, addEventListener(){}, addListener(){}});
globalThis.navigator = {language:"en"};
"""


def _render_rows(rows) -> str:
    """Same harness, but return what #forkRows rendered.

    The sync/release split moved OFF the headline card on 2026-08-06 (owner
    instruction: the card is one number) and ONTO each row, which is where the
    action is anyway -- merging upstream or bumping a pin is per fork, never
    fleet-wide. The #81 principle is unchanged and is asserted here instead.
    """
    return _render_metrics(rows, element="forkRows")


def _render_metrics(rows, element: str = "forkMetrics") -> str:
    """Run build_page's REAL script over `rows`; return what `element` rendered."""
    if not shutil.which("node"):
        pytest.skip("node not available — cannot drive the page's own script")
    bp = _load("build_page")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", bp.PAGE, re.S)
    body = "\n".join(s for s in scripts if s.strip())
    if not body.strip():
        pytest.fail("no script found in build_page.PAGE")
    # ONLY the JS placeholders are substituted. The HTML ones are irrelevant to the
    # arithmetic and left alone, so this cannot accidentally supply a rendered value.
    body = (body.replace("__DATA__", json.dumps(rows))
                .replace("__REPORT__", "{}").replace("__ENH__", "{}")
                # An unsubstituted placeholder is a ReferenceError that kills the
                # WHOLE page script, so this fixture must fill every one. That is
                # how this test caught `__NTOOLS__` being added upstream of it.
                .replace("__NTOOLS__", "59")
                .replace("__PINNOTES__", "{}")
                .replace("__EL__", json.dumps(element)))
    # The element id is interpolated HERE, not through `body.replace` -- this line
    # is appended after the body, so a placeholder substituted into the body never
    # reached it and node died on an undefined name.
    prog = (_DOM + "\n" + body
            + '\n;console.log("\\u0001METRICS\\u0001" + '
            + '(document.getElementById(' + json.dumps(element) + ').innerHTML||""));\n')
    # VIA A FILE, not `node -e`. The real ledger is ~1 MB of JSON and `-e` puts it in
    # argv, which is `OSError: [Errno 7] Argument list too long` — and a harness that
    # dies on the REAL corpus while working on a two-row fixture is the exact way a
    # test ends up only ever proving things about synthetic data.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(prog)
        js = fh.name
    try:
        r = subprocess.run(["node", js], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(js)
    if r.returncode != 0:
        pytest.fail(f"the page's own script did not run: {r.stderr[-1500:]}")
    m = re.search("METRICS(.*)", r.stdout, re.S)
    if not m:
        pytest.fail(f"script ran but #" + element + " was never written: {r.stdout[-800:]}")
    return m.group(1)


def _enrich(rows):
    """Attach the split the way production will: the REAL `lag_split`, real clones."""
    df = _load("discover_forks")
    out = []
    for d in rows:
        d = dict(d)
        clone = FORKS_ROOT / (d.get("tool") or "")
        pin = d.get("pinned_ref_full") or d.get("pinned_ref") or ""
        tip = d.get("ours_unshipped_measured_against") or ""
        up = f"upstream/{d.get('upstream_default_branch') or 'master'}"
        if clone.is_dir() and pin and tip:
            s = df.lag_split(clone, pin, tip, up)
            d["sync_lag"], d["release_lag"] = s["sync_lag"], s["release_lag"]
            d["lag_split_exact"] = s["split_exact"]
        out.append(d)
    return out


def test_the_split_is_measured_on_the_real_fleet():
    """`lag_split` answers on real clones, and never invents a 0 it did not count."""
    rows = _enrich(_real_rows())
    measured = [d for d in rows if isinstance(d.get("sync_lag"), int)
                and isinstance(d.get("release_lag"), int)]
    assert measured, ("lag_split answered for NO fork on the real fleet — either every "
                      "clone is unreadable or the function is not measuring anything")
    for d in measured:
        assert d["sync_lag"] >= 0 and d["release_lag"] >= 0, d.get("tool")
    # A row it could NOT answer must carry None, never 0 — the whole point.
    for d in rows:
        for k in ("sync_lag", "release_lag"):
            assert d.get(k) is None or isinstance(d.get(k), int), (d.get("tool"), k)


def test_page_states_sync_and_release_separately_on_the_real_ledger():
    """The RENDERED page names both halves WHEREVER IT SHOWS A GAP.

    #81's principle, unchanged: sync lag and release lag have OPPOSITE fixes --
    merge upstream in, versus bump the pin and rebuild -- so one number for both
    sends a reader to the wrong action, and twice it did.

    WHERE it is asserted moved on 2026-08-06. It used to be a parenthetical on the
    headline card; the owner asked for the card to be one number, and the card was
    the wrong home anyway for two reasons:

      * `sync + release == behind` is NOT an identity -- a commit between the pin
        and our tip that is ALSO in upstream is counted on both sides -- so the
        card read `28 (sync 14 · release 17)` and the sum was three too big. A
        reader doing the obvious arithmetic concluded the page was broken.
      * the ACTION is per fork. "Merge upstream into yosys" and "bump slang's pin"
        are different jobs on different repos; a fleet-wide sum tells nobody which
        repo to touch.

    So the split lives on the ROW now, next to the fork it is about, and this
    asserts it there. The principle is not weakened: a gap must still say which
    fix it needs.
    """
    html = _render_rows(_enrich(_real_rows()))
    rows = _enrich(_real_rows())
    gap = sum((d.get("sync_lag") or 0) + (d.get("release_lag") or 0)
              for d in rows
              if isinstance(d.get("sync_lag"), int)
              and isinstance(d.get("release_lag"), int))
    if not gap:
        assert "sync " not in html.lower(), (
            "the fleet is level and the rows still print a zero split; the split "
            "exists to tell a reader WHICH fix to apply, and at zero there is no "
            "fix to apply")
        return
    assert "sync" in html.lower(), (
        "the fleet IS behind and no row mentions sync lag. One number for two "
        "conditions with opposite fixes is the defect this test exists for.\n"
        + html[:1200])
    assert "release" in html.lower(), (
        "the fleet IS behind and no row mentions release lag.\n" + html[:1200])


def test_the_card_label_stays_short():
    """The labels carried the full split, every exception and every caveat, and
    nobody read them. An explanation that long is not an explanation; it is
    somewhere for the number to hide.

    Owner instruction 2026-08-05: these two cards say the number and little
    else. Pinned here so the next person adding "just one more clause" has to
    decide to, rather than drift into it.
    """
    src = (HERE / "build_page.py").read_text(encoding="utf-8")
    i = src.index("const kpis = [")
    block = src[i:src.index("\n  ];", i)]
    for needle in ('zh:"落後上游的 commit 數"',
                   'zh:"進到 image 的自有 commit"'):
        assert needle in block, (
            f"the KPI label {needle} has grown clauses again; the split and the "
            f"caveats belong on the NUMBER, where they are one glyph each")


def test_a_release_only_fork_is_not_described_as_behind_upstream():
    """THE INVARIANT, not today's number.

    A fork whose sync lag is 0 and release lag is not needs a PIN BUMP. If the page
    is going to name it at all, it must not name it as trailing upstream — that
    sends the reader to merge, which changes nothing.
    """
    rows = _enrich(_real_rows())
    # CONTENTS ASSERTIONS ARE NOT CLASSIFIED, for the reason vibeic-eda#79/#81
    # established and `ba9587a` encoded: `ARG <TOOL>_VOLUME_CONTENTS_SHA` describes a
    # PREBUILT artefact nothing fetches, so there is no ref for it to be behind and no
    # pin to bump. Calling such a row "release-only" would recreate the retracted
    # 18-commit gap in a new vocabulary. Measured while writing this test: open_pdks
    # counts as release-only on the raw numbers and must not be reported as such.
    ro = [d.get("tool") for d in rows
          if d.get("pin_kind") != "contents_assertion"
          and isinstance(d.get("sync_lag"), int) and isinstance(d.get("release_lag"), int)
          and d["release_lag"] > 0 and d["sync_lag"] == 0]
    if not ro:
        pytest.skip("no release-only fork on the fleet right now — nothing to assert")
    html = _render_rows(rows)
    for tool in ro:
        assert tool in html, (
            f"{tool} is release-only (sync 0, release>0): merging upstream into it "
            f"would change nothing, and the card does not name it as such.\n"
            + html[:1500])


def _rendered(html: str, word: str) -> int:
    m = re.search(rf"{word}\s+(\d+)", html)
    if not m:
        pytest.fail(f"the card never rendered a {word} figure:\n{html[:1200]}")
    return int(m.group(1))


@pytest.mark.parametrize("field,word", [("sync_lag", "sync"), ("release_lag", "release")])
def test_the_rendered_figure_tracks_its_input(field, word):
    """DIFFERENTIAL, and this is the test that has teeth.

    Asserting the card merely CONTAINS the words "sync" and "release" passes against a
    `const syncLag = 0` — measured: with the reduce replaced by a constant, the
    word-presence tests stayed green. That is vibeic-eda#83's defect reproduced in the
    test written to avoid it, and it is why this one exists.

    Nothing here re-implements the page's row filter, which would be the same mistake
    one level down. It changes ONE input by a distinctive amount and requires the
    rendered output to move by exactly that amount. A constant cannot track its input,
    a reduce over the wrong field cannot either, and neither can a sum that quietly
    drops the row. It needs no knowledge of the page's internals at all.
    """
    rows = _enrich(_real_rows())
    # A row the page certainly counts: classified as a real pin, with a measured split.
    target = next((d for d in rows
                   if d.get("pin_kind") != "contents_assertion"
                   and isinstance(d.get("behind_commits"), int)
                   and isinstance(d.get("sync_lag"), int)
                   and isinstance(d.get("release_lag"), int)), None)
    if target is None:
        pytest.skip("no fully-measured pinned row on the fleet right now")
    DELTA = 1000                      # distinctive: no real lag is near it

    # MEASURED AT +DELTA AND +2*DELTA, not at the fleet's current value.
    #
    # The split is rendered only when one of its halves is non-zero (owner
    # instruction 2026-08-05: no `(sync 0 · release 0)` on a clean fleet), so
    # reading the baseline directly fails the moment the fleet reaches the state
    # we are trying to reach -- the test would go red exactly on success.
    #
    # Two bumped renders keep every tooth: both have a figure to read, the
    # difference between them is still exactly DELTA, and a constant, the wrong
    # field, or a sum that drops this row all still fail. It asks the same
    # question from a point where the answer is observable.
    def _at(mult):
        rows2 = [dict(d, **({field: d[field] + DELTA * mult} if d is target else {}))
                 for d in rows]
        return _rendered(_render_rows(rows2), word)

    before, after = _at(1), _at(2)
    assert after - before == DELTA, (
        f"the rendered {word} figure did not track its input: {target.get('tool')}'s "
        f"{field} was raised by {DELTA} and the card moved {after - before}. The number "
        f"is not a sum over the ledger's {field} — it is a constant, the wrong field, "
        f"or a sum that drops this row.")


def test_the_split_is_not_derived_from_behind_commits():
    """`sync + release == behind` is NOT asserted anywhere, because it is not an identity.

    When our line carries upstream commits between the pin and the tip, they are in
    `pin..tip` AND in upstream, and the sum double-counts them. `lag_split` records
    `split_exact` rather than enforcing it; this pins that it is RECORDED, so a later
    hand cannot turn it into an assert that fires at 05:30.
    """
    # PARSED, NOT GREPPED. The first cut of this test searched the function's SOURCE
    # TEXT for "assert " and went red on its own docstring, which explains why the
    # relation must not be asserted. A substring search cannot tell code from the
    # prose about the code; the AST can, and it is the only instrument that answers
    # the question actually being asked.
    import ast
    tree = ast.parse((HERE / "discover_forks.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "lag_split"), None)
    assert fn is not None, "lag_split is gone"
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Assert)], (
        "lag_split now ASSERTS a relation between the halves. `behind == sync + release` "
        "is false whenever our line merged upstream commits between the pin and the tip: "
        "those commits are in `pin..tip` AND in upstream, so the sum double-counts them.")
    assert "split_exact" in ast.dump(fn), (
        "lag_split no longer records whether the split came out exact")
    # Each half is its own call, not arithmetic on the other. Counted on the AST so a
    # comment mentioning `_count` cannot satisfy it.
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_count"]
    assert len(calls) >= 3, (
        f"only {len(calls)} `_count` calls in lag_split: a half is being derived from "
        f"the other rather than measured, so one wrong count corrupts both")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
