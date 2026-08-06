"""The round's verdict must include whether it converged.

WHY THIS EXISTS (measured 2026-08-06)
=====================================
`run_0530.sh` runs `fork_gap_report`, captures its exit code in `GAP`, writes the
measurement to `fork_gap.json` -- and then `GAP` appeared in no condition. The
final verdict was:

    if [ "${SIX}" -ne 0 ] || [ "${TICK}" -ne 0 ] || [ "${DISC}" -ne 0 ] \\
       || [ "${PAGE}" -ne 0 ] || [ "${FRESH}" -ne 0 ] || [ "${CODE}" -ne 0 ] \\
       || [ "${TESTS}" -ne 0 ]; then exit 1; fi

Seven codes, and not the one that says whether the round did its job.

The consequence, measured from the round's own log: `daily_0530 exit 1` on FIVE
consecutive mornings (2026-08-02 through 08-06) while the fleet sat 12 commits
behind upstream. Nothing escalated. The page kept publishing on three of those
days, because publishing is gated on the tests and the tests were green -- so the
one visible artefact said everything was fine while the primary duty was not
being done.

This is the defect family this repo keeps paying for, in the round that exists to
catch it: a check that runs, reports, and cannot influence anything.

WHAT IS AND IS NOT FATAL, AND WHY
---------------------------------
SYNC lag fatal. "Merge upstream into our line" is the round's primary duty. If it
finishes with a fork still behind upstream, it did not do that, whatever else
went right.

RELEASE lag named but not fatal. A pin trailing our own fork tip closes with a
rebuild -- and `daily_release` DELIBERATELY declines to move a pure mirror's pin,
because ORFS and slang carry none of our commits and advancing them is a
gatekeeper decision. Failing the round on a decision reserved for a human would
make it permanently red, and a permanently red round is one people route around.

UNMEASURED is fatal too, and separately: `fork_gap_report` exits 2 when it cannot
answer, and "we could not tell whether we converged" is not "we converged".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROUND = HERE / "run_0530.sh"


def _verdict_block() -> str:
    """The final `if ...; then exit 1; fi` and the lines that feed it."""
    s = ROUND.read_text(encoding="utf-8")
    i = s.rindex('if [ "${SIX}"')
    return s[max(0, i - 3000):]


def test_the_verdict_consults_the_convergence_measurement():
    """THE REGRESSION. Seven exit codes were consulted and the gap was not."""
    block = _verdict_block()
    cond = block[block.rindex('if [ "${SIX}"'):]
    cond = cond[:cond.index("then exit 1")]
    assert re.search(r"GAP_FATAL|SYNC_LAG", cond), (
        "the round's final verdict does not consult the convergence "
        "measurement. `fork_gap_report` runs, its result is captured, and the "
        "verdict ignores it -- so a round that leaves the fleet behind upstream "
        f"reports success. Condition was:\n{cond}")


def test_sync_lag_is_read_from_the_measurement_not_recomputed():
    """It must read `fork_gap.json`, the artefact the measurement wrote.

    Recomputing the gap in shell would be a second implementation of the
    question, free to disagree with the one the report publishes -- which is how
    this fleet ended up with a page saying 96 while the live answer was 26.
    """
    block = _verdict_block()
    assert "fork_gap.json" in block, (
        "the convergence gate does not read fork_gap.json; a second, independent "
        "computation of the same number is free to disagree with the published one")
    assert "q1_sync_lag" in block, (
        "the gate does not read `q1_sync_lag` specifically. Sync lag and release "
        "lag have OPPOSITE fixes and only one of them is this round's duty")


def test_an_unmeasured_gap_is_fatal_and_not_treated_as_zero():
    """THE THIRD STATE. `fork_gap_report` exits 2 when it cannot answer, and it
    did exactly that this morning. 'We could not tell whether we converged' must
    not read as 'we converged'."""
    block = _verdict_block()
    assert "unmeasured" in block, (
        "there is no unmeasured branch: a missing or unparseable fork_gap.json "
        "would fall through as if the lag were zero")
    i = block.index("unmeasured")
    after = block[i:i + 600]
    assert "GAP_FATAL=1" in after, (
        "the unmeasured branch does not make the round fail; measuring nothing "
        "proves nothing, and this repo's oldest rule is that MISSING is not a pass")


def test_release_lag_alone_does_not_fail_the_round():
    """BIDIRECTIONAL CONTROL. Making everything fatal is not a fix -- it is an
    outage. `daily_release` reserves the pure-mirror pin move for a human on
    purpose, so a release gap must be reported without turning the round red."""
    block = _verdict_block()
    assert "q1_release_lag" not in block.split("GAP_FATAL=0")[0], (
        "release lag feeds the fatal path. A pure mirror's pin move is a "
        "gatekeeper decision by design (ORFS and slang carry none of our "
        "commits), so this would make the round permanently red for doing the "
        "right thing")


@pytest.mark.parametrize("lag,want_fatal", [("0", 0), ("12", 1), ("unmeasured", 1)])
def test_the_gate_behaves_on_each_state(tmp_path, lag, want_fatal):
    """Run the gate's own logic, rather than reading it.

    The shell is extracted and executed with a synthetic fork_gap.json, so this
    asserts what the round WOULD do, not what the source looks like.
    """
    state = tmp_path / "state"
    state.mkdir()
    if lag != "unmeasured":
        (state / "fork_gap.json").write_text(json.dumps({"q1_sync_lag": int(lag)}))

    block = _verdict_block()
    start = block.index("SYNC_LAG=$(")
    end = block.index("# 0 only when BOTH are clean")
    gate = block[start:end]

    script = (f'STATE="{state}"\nLOG=/dev/null\nGAP=1\n' + gate
              + '\necho "GAP_FATAL=${GAP_FATAL}"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "HOME": str(tmp_path)})
    m = re.search(r"GAP_FATAL=(\d+)", r.stdout)
    assert m, f"the gate produced no verdict: {r.stdout!r} {r.stderr!r}"
    assert int(m.group(1)) == want_fatal, (
        f"sync lag {lag!r} produced GAP_FATAL={m.group(1)}, expected "
        f"{want_fatal}. stdout={r.stdout!r}")
