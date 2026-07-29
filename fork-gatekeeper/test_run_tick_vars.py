#!/usr/bin/env python3
"""`run_tick.sh` runs under `set -u`, so a variable assigned only inside a
conditional branch and read outside it is not a skip — it aborts the whole tick.

Hit exactly once: `REL_VER`/`REACH_IMG` were derived inside
`if [ -f fork_reaches_flow_check.py ]` while the capability sweep and the
image-provenance guard both read `${REACH_IMG}` afterwards. A missing checker
would have taken the gatekeeper round down with it:

    $ bash -c 'set -uo pipefail; if false; then X="v"; fi; [ -n "${X}" ]'
    bash: line 1: X: unbound variable

The check is indentation-based, which is a proxy rather than a parse — good
enough for this file's shape and honest about what it is. Validated against the
pre-fix revision, where it flags REACH_IMG.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TICK = Path(__file__).resolve().parent / "run_tick.sh"


def _scan(lines):
    assign, use = {}, {}
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)=", ln)
        if m:
            assign.setdefault(m.group(2), []).append((i, len(m.group(1))))
        for u in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", ln):
            use.setdefault(u.group(1), []).append((i, len(ln) - len(ln.lstrip())))
    bad = []
    for v, uses in use.items():
        a = assign.get(v)
        if not a or not all(ind > 0 for _, ind in a):
            continue
        depth = min(ind for _, ind in a)
        outer = [i for i, ind in uses if ind < depth]
        if outer:
            bad.append((v, a[0][0], outer[0]))
    return bad


def test_no_variable_is_assigned_in_a_branch_and_read_outside_it():
    bad = _scan(TICK.read_text().splitlines())
    assert not bad, "under `set -u` these abort the tick, they do not skip: " + \
        "; ".join(f"${v} assigned line {ai}, read line {ui}" for v, ai, ui in bad)


def test_the_check_catches_the_revision_it_was_written_for():
    """A guard that cannot fail is not a guard — some past revision must trip it.

    Pinned to `HEAD~2` at first, which was true for exactly two commits and then
    failed as a red suite on unrelated work. A test anchored to a MOVING relative
    revision measures how much has landed since, not the thing it names. It now
    walks this file's own history and asserts that at least one past revision
    carried the defect, which stays true however far HEAD moves.
    """
    revs = subprocess.run(
        ["git", "log", "--format=%H", "--", "fork-gatekeeper/run_tick.sh"],
        capture_output=True, text=True, cwd=str(TICK.parent.parent))
    shas = [s for s in revs.stdout.split() if s][:40]
    if not shas:
        import pytest
        pytest.skip("no history for run_tick.sh in this checkout")
    for sha in shas:
        old = subprocess.run(
            ["git", "show", f"{sha}:fork-gatekeeper/run_tick.sh"],
            capture_output=True, text=True, cwd=str(TICK.parent.parent))
        if old.returncode == 0 and any(
                v == "REACH_IMG" for v, _, _ in _scan(old.stdout.splitlines())):
            return
    raise AssertionError(
        "no revision of run_tick.sh in the last %d trips the check — either the "
        "history is shallow or the check stopped working" % len(shas))


def test_the_tick_still_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0


# --- vibe-ic#550 extended: this repo is under the same account-level block

def test_ci_out_is_defined_before_the_branch_that_reads_it():
    """`set -u` turns a conditional assignment read outside into an abort.

    This file exists because REACH_IMG was assigned inside an `if` and read
    after it, which under `set -uo pipefail` aborts the whole tick instead of
    skipping a step. The CI-ran block has the same shape, so it gets the same
    check rather than the same bug.
    """
    src = TICK.read_text()
    assign = src.index('CI_OUT="')
    for use in (m.start() for m in re.finditer(r'\$\{CI_OUT\}', src)):
        assert use > assign, \
            "CI_OUT is read before it is assigned — under set -u that aborts the tick"


def test_the_ci_check_is_reused_not_reimplemented():
    """Two implementations of 'did CI run' is how two programs disagree.

    vibeic-eda#29 was exactly that defect one layer over: check_pins_current and
    daily_release each carried their own copy of `branch_is_ours` and said
    opposite things about the same four pins.
    """
    src = TICK.read_text()
    assert "ci_ran_at_all_check.py" in src
    assert "VIBE_IC_PROGRAMS" in src, \
        "the path must be overridable, or a checkout elsewhere silently skips it"
    # …and the tick must not grow its own version of the logic. CODE only:
    # `actions/runs` appears twice in COMMENTS, recording the measurement that
    # established the block, and a check that cannot tell a recorded fact from
    # a reimplementation would have to be weakened the first time someone
    # documented something — which is how a gate becomes decorative.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "actions/runs" not in code, \
        "the tick is reimplementing the API query instead of calling the checker"


def test_a_missing_checker_reports_rather_than_passing():
    """A guard that cannot run is not a guard that passed — the rule this file
    already enforces for the source guards."""
    src = TICK.read_text()
    i = src.index("CI_CHECK=")
    block = src[i:i + 1400]
    assert "MISSING:" in block
    assert "not a clean result" in block


def test_the_ci_finding_does_not_fail_the_tick():
    """Deliberate, and worth pinning so nobody 'fixes' it into daily noise.

    The block is account-level: no tick can re-enable Actions, so failing every
    tick on it would train the operator to ignore the tick. It is logged loudly
    and left non-fatal, the same treatment the source guards get for the same
    reason.
    """
    src = TICK.read_text()
    i = src.index("CI_CHECK=")
    block = src[i:i + 1400]
    assert "ci_rc=$?" in block
    assert "guard_rc=1" not in block, \
        "the CI finding must not be folded into the tick's failure code"


def test_every_checker_in_this_directory_is_called_by_something():
    """A gate that exists and is never invoked is the same as no gate.

    `check_image_claims.py` landed in 6f04ab6 with PDKS.json, tests and a
    baseline — and nothing ever called it. It sat unwired through four releases,
    and the defect it exists to catch (a dependency nobody declared) is exactly
    the shape of the defect it WAS: a program nobody wired.

    Found by sweeping every program this session added and asking what invokes
    it, not by anything failing. So the sweep becomes a test.

    Stated limit: this asks whether a name appears in an invoking file, which
    catches "nobody calls it at all" and not "called with the wrong arguments".
    That is the failure that actually happened, twice.
    """
    # EVERY sibling and every shell script, not a hand-listed few. My first
    # version named three callers and flagged `_nda_tokens` and `reachability`,
    # which are imported by `build_page.py` and `assess_release.py` — files not
    # on my list. A sweep with a hand-maintained scope finds what the author
    # remembered, which is the failure it is looking for.
    callers = []
    for f in sorted(TICK.parent.glob("*.py")) + sorted(TICK.parent.glob("*.sh")):
        if f.name.startswith("test_"):
            continue
        callers.append((f.name, f.read_text(errors="replace")))
    for f in sorted((TICK.parent.parent / "tools").glob("*.py")):
        callers.append((f.name, f.read_text(errors="replace")))
    assert callers, "found no caller files — the sweep examined nothing"

    #: Modules that are libraries or entry points rather than gates. Each is a
    #: decision someone can point at, which is the whole argument of #28.
    NOT_A_GATE = {
        "run_tick", "gatekeeper", "daily_merge", "daily_release", "assess",
        "assess_release", "llm_judge", "build_page", "inventory",
        "check_pins_current",      # imported by daily_release, not shelled out
        "_gate_denominator", "_record_adjudication",
        # HUMAN-INVOKED BY DESIGN, and the distinction matters. Its own
        # docstring says "before you land ANY fork PR, run this" — it takes a
        # repo and a PR number, so a tick with no PR in hand has nothing to
        # give it. Unwired-and-intended is a different state from
        # unwired-and-forgotten, and this set is where that gets written down
        # rather than inferred from silence.
        "pr_precheck",
    }

    missing = []
    for f in sorted((TICK.parent).glob("*.py")):
        stem = f.stem
        if stem.startswith("test_") or stem in NOT_A_GATE:
            continue
        # …and a file does not count as its own caller.
        if not any(stem in text for name, text in callers if name != f.name):
            missing.append(f.name)
    assert not missing, (
        "these live in fork-gatekeeper/ and nothing invokes them, so they have "
        "never run: " + ", ".join(missing))
