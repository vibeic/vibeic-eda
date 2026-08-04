"""The page's lag total must not count a CONTENTS ASSERTION as a gap.

Measured 2026-08-04: the published page reported 39 commits behind across 7 forks.
18 of those came from one row whose `pin_kind` is `contents_assertion` — nothing
clones at that ref, the build refuses if the shipped artefact disagrees, and
advancing it rebuilds nothing. Two advancement proposals were raised and refused
on that number before anyone noticed it was not a gap.

The ledger has always carried `pin_kind`. The page simply never read it.

Both directions are asserted. Testing only that assertions are excluded would pass
against a page that excluded everything; the second case pins the other side.
"""
import json, re, shutil, subprocess, pathlib, pytest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "build_page.py"

ROWS = [
    {"tool": "assertion_only", "pin_kind": "contents_assertion", "behind_commits": 18},
    {"tool": "real_lag_a",     "pin_kind": "pin",                "behind_commits": 12},
    {"tool": "real_lag_b",     "pin_kind": "pin",                "behind_commits": 3},
    {"tool": "current",        "pin_kind": "pin",                "behind_commits": 0},
    {"tool": "unmeasurable",   "pin_kind": "pin",                "behind_commits": None},
]


def _eval(rows):
    """Run the page's own lag arithmetic, lifted verbatim from build_page.py."""
    src = SRC.read_text(encoding="utf-8")
    block = re.search(
        r"(const assertionRows.*?const forksBehind\s*=.*?;|"
        r"const behindKnown\s*=.*?const forksBehind\s*=.*?;)", src, re.S)
    assert block, "could not lift the lag arithmetic out of build_page.py"
    js = ("const gapRows = %s;\n%s\n"
          "console.log(JSON.stringify({commitsBehind, forksBehind}));"
          % (json.dumps(rows), block.group(1)))
    if shutil.which("node") is None:
        pytest.skip("node not available — cannot evaluate the page's own expression")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_contents_assertion_is_not_counted_as_lag():
    got = _eval(ROWS)
    assert got["commitsBehind"] == 15, got   # 12 + 3, not 12 + 3 + 18
    assert got["forksBehind"] == 2, got      # two real ones, not three


def test_real_lag_is_still_counted():
    """The other direction: a fix that excluded everything would pass the first test."""
    got = _eval([r for r in ROWS if r["pin_kind"] != "contents_assertion"])
    assert got["commitsBehind"] == 15, got
    assert got["forksBehind"] == 2, got
