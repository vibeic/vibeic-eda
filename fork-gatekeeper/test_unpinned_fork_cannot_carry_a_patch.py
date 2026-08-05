"""A fork with no pin that is AHEAD is carrying a patch that cannot ship. #60.

`open_pdks`, `ciel`, `sv2v` and `IHP-Open-PDK` are forks the image does not build
from — no `ARG <TOOL>_REF`, no clone, the base image's copies. All four carry
ZERO patches today, so nothing is lost, and that is exactly why it is easy to
miss: the defect activates on the first patch, and at that moment the ledger
reports `ahead=1` truthfully and the row reads like success.

TWO CONDITIONS, DELIBERATELY SEPARATE:

* not built from ours — a standing, owner-pending state (#60 states both
  resolutions, wire-in or stop-forking, and declines to pick). Recorded as a
  baseline that MAY ONLY SHRINK. Without it the report is permanently rc=1 for
  a reason nobody is acting on, and a report that is always red is one people
  route around — which would hide the condition below on the day it first
  becomes true.
* ahead > 0 with no pin — the contradiction. NO baseline excuses it: being on a
  known list excuses NOT BEING BUILT FROM, not carrying an unshippable patch.

MEASURED, all four directions:

    the four, zero patches            rc=0
    open_pdks ahead=1, no pin         rc=1   "CANNOT SHIP"
    a NEW unpinned fork               rc=1   "newly not built"
    one got pinned (baseline shrank)  rc=0   "baseline shrank"

AND (vibeic-eda#79) THE THIRD ROW STATE THIS REPORT NOW HAS. It is where the two
refused open_pdks bumps came from: `fork_gap_report` read "18 commits
image-behind-upstream" and #74 and #78 each cited that line. A CONTENTS ASSERTION
has no ref to be behind — the artefact is prebuilt and the build only asserts
what it carries — so such a row is a fact, never a gap, and never NOT MEASURED
either. The end of this file measures all three directions on a synthetic fork.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "fork_gap_report", _HERE / "fork_gap_report.py")
F = importlib.util.module_from_spec(_spec)
sys.modules["fork_gap_report"] = F
try:
    _spec.loader.exec_module(F)
except SystemExit:
    pass

_FIELDS = dict(integrated=False, ahead=0, image_behind=None, sync_lag=None,
               release_lag=None, pin=None, ours_unshipped=None,
               ours_unshipped_substantive=None, unshipped_commits=[],
               note="", pin_disagreement=None,
               kind="pin", asserted_contents=None,
               tip_state=None, tip_note=None)


def _run(rows, monkeypatch, baseline=None):
    rep = {
        "q1_image_behind_upstream": 0, "q1_forks_behind": 0, "q1_sync_lag": 0,
        "q1_release_lag": 0, "q1_unmeasured": [], "q2_unmeasured_ship": [],
        "q2_forks_not_built_from_ours": [r["tool"] for r in rows
                                         if not r["integrated"]],
        "q2_our_commits_stranded": sum(r.get("ahead") or 0 for r in rows
                                       if not r["integrated"]),
        "q2_ours_past_the_pin": 0, "q2_ours_past_the_pin_substantive": 0,
        "q2_unshipped_commits": [], "rows": rows,
        # `analyse`'s contract, modelled faithfully rather than worked around.
        # `main` reads this key directly: a stub that omits it would be telling
        # the exit-ladder tests that a shape `analyse` never produces is fine.
        "assertions": [{"tool": r["tool"], "contents": r["asserted_contents"]}
                       for r in rows if r.get("kind") == "contents_assertion"],
        # Same reason, same derivation `analyse` uses (vibeic-eda#92). `main`
        # reads both directly and `tip_rejected` participates in the exit ladder,
        # so a stub that omitted them would be quietly testing a different `main`.
        "tip_rejected": [{"tool": r["tool"], "why": r.get("tip_note")}
                         for r in rows if r.get("tip_state") == F.TIP_BEHIND],
        "tip_unverified": [{"tool": r["tool"], "why": r.get("tip_note")}
                           for r in rows if r.get("tip_state") == F.TIP_UNDETERMINED],
    }
    monkeypatch.setattr(F, "_UNPINNED_BASELINE",
                        _SYNTHETIC_BASELINE if baseline is None else baseline)
    monkeypatch.setattr(F, "analyse", lambda *a, **k: rep)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = F.main(["--no-fetch"])
    return rc, buf.getvalue()


#: A SYNTHETIC baseline. These tests used to build their rows from the live
#: `F._UNPINNED_BASELINE`, so the day that register emptied — the day the debt was
#: PAID — `_baseline_rows()` returned [] and the mechanism tests became vacuous or
#: raised IndexError. Paying off debt must not delete the guard that catches the
#: next case. The mechanism is what is under test here; the live contents are
#: pinned separately, once, below.
_SYNTHETIC_BASELINE = frozenset({"tool_a", "tool_b", "tool_c"})


def _baseline_rows():
    return [dict(_FIELDS, tool=t) for t in sorted(_SYNTHETIC_BASELINE)]


# ── the standing state is not a finding ───────────────────────────────────
def test_the_recorded_four_with_no_patches_are_not_a_finding(monkeypatch):
    """THE ACCEPT CASE, and the one that decides whether this can hold: a report
    that is always red is one people route around."""
    rc, _out = _run(_baseline_rows(), monkeypatch)
    assert rc == 0


def test_the_baseline_is_empty_because_all_four_are_now_pinned():
    """The register MAY ONLY SHRINK, and it has shrunk to nothing.

    open_pdks, ciel, sv2v and IHP-Open-PDK each carry a real `ARG <TOOL>_REF`
    now. Asserted here so re-adding a name is a deliberate, visible act rather
    than a quiet re-accrual — a debt register that can grow again silently is
    a waiver list."""
    assert F._UNPINNED_BASELINE == frozenset(), (
        f"the unpinned baseline gained entries: {sorted(F._UNPINNED_BASELINE)}. "
        f"A fork that is not built from is a finding, not a new baseline row.")


# ── the contradiction, which no baseline excuses ──────────────────────────
def test_a_baselined_fork_that_gains_a_patch_still_FAILS(monkeypatch):
    """LOAD-BEARING. Being on the list excuses NOT BEING BUILT FROM. It does not
    excuse carrying a patch that cannot reach a user — and this is the exact
    moment #60 exists to catch, currently unreachable, which is why guarding it
    now is cheap."""
    rows = _baseline_rows()
    rows[0]["ahead"] = 1
    rc, out = _run(rows, monkeypatch)
    assert rc == 1
    assert "CANNOT SHIP" in out
    assert rows[0]["tool"] in out


def test_the_stranded_check_runs_BEFORE_the_baseline_is_consulted():
    """Order matters: if the baseline were consulted first and returned 0, a
    patched-and-unbuilt fork would pass on the strength of being known."""
    src = (_HERE / "fork_gap_report.py").read_text(encoding="utf-8")
    i = src.index("_stranded_rows = [")
    j = src.index("_unpinned = set(", i)
    assert i < j, "the baseline is consulted before the contradiction"


# ── the baseline may only shrink ──────────────────────────────────────────
def test_a_NEW_unpinned_fork_is_a_finding(monkeypatch):
    rows = _baseline_rows() + [dict(_FIELDS, tool="brand_new")]
    rc, out = _run(rows, monkeypatch)
    assert rc == 1 and "newly not built" in out


def test_a_SHRUNK_baseline_is_reported_and_passes(monkeypatch):
    """Shrinking is the outcome this exists to make visible — it must not be
    silent, or the list stays stale after the work is done."""
    rc, out = _run(_baseline_rows()[:-1], monkeypatch)
    assert rc == 0 and "baseline shrank" in out


def test_the_repo_as_committed_has_no_RECORDING_inconsistency():
    """The regression: whatever the real state is, it must be the recorded one.

    ASSERTS THE DOCSTRING, NOT rc==0. This required exit 0, and the report's
    final line is `rc 0 iff q1_image_behind_upstream == 0`. Being behind upstream
    is the ORDINARY state between releases, so exit 0 means "green only in the
    instant after a release that took every upstream commit" — and it is not
    reachable at all while open_pdks is deliberately not advanced (vibeic-eda#79
    — that ARG is an assertion about a prebuilt volume, not a pin, so "behind" is
    not a question it can answer). The test asserted a condition the repo cannot
    satisfy by design, and had been red long enough to be filed as a mystery.

    What "recorded == real" actually means here is the three INCONSISTENCY
    conditions, each of which is a genuine defect rather than a passage of time:

        CANNOT SHIP        a fork carries a patch with no pin to ship it through
        newly not built    a fork stopped being built from, unrecorded
        baseline shrank    the register lists forks that are now pinned

    Ordinary lag is reported by the run and is not one of them.
    """
    import subprocess
    r = subprocess.run([sys.executable, str(_HERE / "fork_gap_report.py"),
                        "--no-fetch"], capture_output=True, text=True,
                       timeout=180, cwd=str(_HERE.parent))
    out = r.stdout + r.stderr
    assert r.returncode != 2, f"the report could not measure:\n{out[-600:]}"
    for marker in ("CANNOT SHIP", "newly not built", "baseline shrank"):
        assert marker not in out, (
            f"recorded state disagrees with real state — {marker!r}:\n{out[-800:]}")


# ── vibeic-eda#79 — an ASSERTION is not a pin, and the sweep must know it ──
#
# THE REAL `analyse`, not a stubbed report. Everything above monkeypatches
# `analyse` away, which is right for testing the exit ladder and useless here:
# the defect #79 is about lives INSIDE `analyse` — in which ARG it reads and
# which fallback it takes when it finds none. A test that stubbed it out would
# pass straight over the bug.
import json as _json                                             # noqa: E402
import subprocess as _sp                                         # noqa: E402


def _git(cwd, *a):
    r = _sp.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                timeout=60)
    assert r.returncode == 0, f"git {' '.join(a)} -> {r.stderr}"
    return r.stdout.strip()


def _init(d):
    _sp.run(["git", "init", "-q", str(d)], check=True, timeout=60)
    _git(d, "config", "user.email", "t@example.invalid")
    _git(d, "config", "user.name", "t")


def _fixture(tmp_path, arg_name):
    """A repo whose Dockerfile declares `demo` under `arg_name`, a `demo` clone
    two commits behind its upstream, and a ledger row for it.

    The declared value is commit 1 and upstream is at commit 3, so a row treated
    as a PIN reads 2 behind. That number is the whole signal — it is the shape of
    the "18 commits image-behind-upstream" that #74 and #78 each acted on.
    """
    repo = tmp_path / "eda"
    (repo / "tools").mkdir(parents=True)
    _init(repo)

    clone = tmp_path / "forks" / "demo"
    clone.mkdir(parents=True)
    _init(clone)
    shas = []
    for i in range(3):
        (clone / f"f{i}").write_text(str(i))
        _git(clone, "add", f"f{i}")
        _git(clone, "commit", "-qm", f"c{i}")
        shas.append(_git(clone, "rev-parse", "HEAD"))
    # `origin/master` is the published tip; `upstream/master` is where upstream
    # is. Both are remote-tracking refs the report resolves by name.
    _git(clone, "update-ref", "refs/remotes/origin/master", shas[2])
    _git(clone, "update-ref", "refs/remotes/upstream/master", shas[2])

    (repo / "Dockerfile").write_text(
        f"ARG {arg_name}={shas[0]}\n"
        "FROM ubuntu:24.04\n"
        f"ARG {arg_name}\n"
        f'RUN readlink -f /x | grep -q "${{{arg_name}}}"\n')
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", "c")
    _git(repo, "update-ref", "refs/remotes/origin/main",
         _git(repo, "rev-parse", "HEAD"))

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "demo.json").write_text(_json.dumps({
        "tool": "demo", "integrated": True, "ahead": 0,
        # PRESENT ON PURPOSE. Renaming the ARG off `_REF` alone does NOT fix
        # this: `analyse` falls back to the ledger's own `pinned_ref_full`, it
        # resolves in the clone, and the row comes back reading 2 behind exactly
        # as before. The assertion branch has to run BEFORE that fallback, and
        # this field is what makes the test able to tell the difference.
        "pinned_ref_full": shas[0], "pinned_ref": shas[0][:12],
    }))
    return repo, tmp_path / "forks", ledger, shas


def test_a_REF_named_arg_is_still_swept(tmp_path):
    """THE NEGATIVE CONTROL. Without it "no gap reported" proves nothing — a
    fixture that measures nothing also reports no gap."""
    repo, forks, ledger, _ = _fixture(tmp_path, "DEMO_REF")
    rep = F.analyse(repo, forks, ledger, fetch=False)
    row = rep["rows"][0]
    assert row["kind"] == "pin"
    assert row["image_behind"] == 2, row
    assert rep["q1_image_behind_upstream"] == 2


def test_a_contents_assertion_is_not_swept(tmp_path):
    """THE #79 REGRESSION. Same fixture, same clone, same ledger — ONLY the ARG
    NAME differs, which is the point: the rule is mechanical, so a sweep applies
    it without anyone remembering."""
    repo, forks, ledger, shas = _fixture(tmp_path, "DEMO_VOLUME_CONTENTS_SHA")
    rep = F.analyse(repo, forks, ledger, fetch=False)
    row = rep["rows"][0]
    assert row["kind"] == "contents_assertion"
    assert row["image_behind"] is None
    assert row["asserted_contents"] == shas[0]
    assert rep["q1_image_behind_upstream"] == 0
    assert rep["q1_forks_behind"] == 0


def test_it_is_not_swept_and_not_NOT_MEASURED_either(tmp_path):
    """The second half, and the one easy to get wrong. Replacing a false gap with
    a false "could not measure" exits 2 every morning, and a permanently red
    report is one people route around (#17)."""
    repo, forks, ledger, shas = _fixture(tmp_path, "DEMO_VOLUME_CONTENTS_SHA")
    rep = F.analyse(repo, forks, ledger, fetch=False)
    assert "demo" not in rep["q1_unmeasured"]
    assert "demo" not in rep["q2_unmeasured_ship"]
    assert "demo" not in rep["q2_forks_not_built_from_ours"]
    assert rep["assertions"] == [{"tool": "demo", "contents": shas[0]}]


def test_the_fact_it_carries_is_still_reported(tmp_path):
    """A row that VANISHES is a row nobody can audit. WHICH upstream commit the
    shipped artefact contains is the whole reason the ARG exists, so the sweep
    keeps stating it — it just stops calling it a gap."""
    repo, forks, ledger, shas = _fixture(tmp_path, "DEMO_VOLUME_CONTENTS_SHA")
    rep = F.analyse(repo, forks, ledger, fetch=False)
    note = rep["rows"][0]["note"]
    assert "CONTENTS ASSERTION" in note and shas[0][:12] in note
    assert "rebuild" in note
