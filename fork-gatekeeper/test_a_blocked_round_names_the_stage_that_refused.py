#!/usr/bin/env python3
"""Three rounds reported "release failed" for a release that was never attempted.

`run_tick` assigns a DIFFERENT exit code per stage, and exactly one of them —
5 — is a release failure. Exits 3/4/8/9 force `blocking_rc`, which makes the
script REFUSE to invoke `daily_release` at all; `run_tick.sh` says so in its own
comment at the refusal ("This is not a daily_release failure: no release was
attempted") and deletes `daily-release.json` there so yesterday's success cannot
be read as today's evidence.

`check_release_failed` knew none of that. It called every non-zero exit a release
failure and read one file, `daily-release.json`. So the 2026-08-25, 2026-09-07 and
2026-09-08 rounds — all exit 4, all `daily_merge` — produced this, three times,
BYTE-IDENTICAL (verified: the 2026-09-08 alert on PR #197 is reproduced exactly by
`_report(4, "ran", …)` over an absent release record):

    ## What the release said
    ```
    (the release record could not be read — …/daily-release.json does not exist)
    ```
    ## What to check
    - The archived build output under `.cache/eda-fork-gatekeeper/failed-releases/`.

Every line of that is wrong for exit 4. Nothing was built, so there is no build
output; `failed-releases/` is written only on the branch where a build ran, so it
holds nothing for this class; and the record that DID hold the diagnosis —
`daily-merge.json`, one row per fork with the state that stopped each one — was
never opened.

WHAT THESE TESTS DRIVE. `main()` with `_default_escalate` captured, so the BODY
that would be published is asserted, not just the exit code. No test here names a
symbol this change adds: against pristine `check_release_failed.py` they fail on
the assertion, because the old code runs them perfectly well and answers wrongly.
The exit-5 tests are the control — that path is unchanged and must stay so.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROUND_AT = "2026-09-08T05:30:01+08:00"

# The shape `daily_merge.merge_one` actually writes, trimmed to the fields the
# alarm has to read. CONFLICT_NEEDS_HUMAN is one of the states that makes
# daily_merge exit 1, which makes run_tick exit 4.
_MERGE_RECORD = {
    "program": "daily_merge",
    "forks": [
        {"repo": "yosys", "branch": "main", "state": "MERGED",
         "detail": "took 3 upstream commit(s); 11 of ours still ahead",
         "conflicts": [], "checks": [], "took": 3},
        {"repo": "klayout", "branch": "master", "state": "ALREADY_CURRENT",
         "detail": "level with upstream/master", "conflicts": [], "checks": [],
         "took": 0},
        {"repo": "OpenROAD", "branch": "master", "state": "CONFLICT_NEEDS_HUMAN",
         "detail": "2 file(s) conflicted; merge aborted, fork left at its "
                   "previous tip",
         "conflicts": ["src/rcx/src/extmain_v2.cpp", "src/rcx/src/ext.i"],
         "checks": [], "took": 41},
        {"repo": "Trilinos", "branch": "vibeic/xyce-trilinos-17.2-epetra-restored",
         "state": "POST_MERGE_CHECK_FAILED",
         "detail": "1 of 1 post-merge check(s) returned a complete red result; "
                   "NOT pushed, fork left at its previous tip",
         "conflicts": [],
         "checks": [{"name": "anasazi-epetra-builds", "ok": False, "rc": 2,
                     "outcome": "natural",
                     "detail": "No rule to make target 'anasaziepetra'"}],
         "took": 7},
    ],
}

_MERGE_TXT = (
    "daily_merge: 38 fork(s), 1 merged, 3 upstream commit(s) taken, "
    "2 need a human, 1 blocked by a post-merge check\n"
    "  OpenROAD       CONFLICT_NEEDS_HUMAN   2 file(s) conflicted\n"
    "  Trilinos       POST_MERGE_CHECK_FAILED 1 of 1 post-merge check(s) red\n"
)

# The release record from the 2026-08-30 round, which is exit 5's shape.
_RELEASE_RECORD = {
    "program": "daily_release",
    "status": "unmeasured",
    "failure": {
        "stage": "artefact_discovery",
        "category": "registry_artefact_state_unmeasurable",
        "retryable": True,
        "subject": "ghcr.io/vibeic/eda-tool-xyce:1fab458-71dfca",
        "summary": "registry presence unmeasured: net/http: TLS handshake timeout",
    },
}


def _mod():
    spec = importlib.util.spec_from_file_location(
        "crf_under_test", HERE / "check_release_failed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def merge_refused(tmp_path, monkeypatch):
    """A state dir exactly as the 2026-09-08 round left it: daily_merge's two
    records present, and `daily-release.json` ABSENT because the round deleted
    it at the refusal."""
    state = tmp_path / "gk-state"
    state.mkdir()
    (state / "daily-merge.json").write_text(
        json.dumps(_MERGE_RECORD, indent=2) + "\n", encoding="utf-8")
    (state / "daily-merge.txt").write_text(_MERGE_TXT, encoding="utf-8")
    (state / "daily-release.txt").write_text(
        "BLOCKED: source guards, daily merge, vendored closure, or fork "
        "self-checks are not green; daily_release was not invoked\n",
        encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(state))
    return state


def _raise(m, monkeypatch, *args):
    """Call main() and return (rc, summary, body) without opening anything."""
    seen = {}

    def _capture(summary, report_md, round_at="undated", kind="release-failed"):
        seen["summary"], seen["body"] = summary, report_md
        return True, "captured by the test"

    monkeypatch.setattr(m, "_default_escalate", _capture)
    rc = m.main(["--round-at", ROUND_AT, *args])
    return rc, seen.get("summary", ""), seen.get("body", "")


# --- the case this exists for ------------------------------------------------

def test_the_alert_names_the_stage_that_actually_refused(merge_refused,
                                                         monkeypatch):
    """THE REGRESSION, verbatim: exit 4 on a round whose release record is gone
    because the round deleted it. The old code said the release record could not
    be read; there is nothing wrong with the release record, there was no
    release."""
    m = _mod()
    rc, summary, body = _raise(m, monkeypatch, "--tick-exit", "4")
    assert rc == m.RC_ESCALATED
    assert "daily_merge" in body, body
    assert "the release record could not be read" not in body, body
    assert not summary.startswith("release failed"), summary


def test_the_alert_carries_the_forks_that_refused(merge_refused, monkeypatch):
    """`daily-merge.json` holds one row per fork with the state that stopped it.
    Those rows ARE the diagnosis, and the population that made the program exit 1
    is exactly the rows outside MERGED/ALREADY_CURRENT/WOULD_MERGE."""
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    assert "OpenROAD" in body and "CONFLICT_NEEDS_HUMAN" in body, body
    assert "src/rcx/src/extmain_v2.cpp" in body, body
    assert "Trilinos" in body and "POST_MERGE_CHECK_FAILED" in body, body
    assert "anasazi-epetra-builds" in body, body


def test_the_clean_forks_are_not_reported_as_findings(merge_refused, monkeypatch):
    """The other tail. 36 of 38 forks were fine; listing them turns the alert
    into a wall nobody reads, which is how an alarm gets ignored."""
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    detail = body.split("## What daily_merge said")[1].split("```")[1]
    assert "yosys" not in detail, detail
    assert "ALREADY_CURRENT" not in detail, detail


def test_it_does_not_send_the_reader_to_an_archive_this_class_never_writes(
        merge_refused, monkeypatch):
    """`failed-releases/` is written inside the `release_rc != 0` branch only. For
    exit 4 that branch never ran, so the one instruction the three identical
    alerts gave pointed at an empty directory."""
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    assert ("The archived build output under "
            "`.cache/eda-fork-gatekeeper/failed-releases/`.") not in body, body
    assert "daily-merge.txt" in body, body


def test_it_says_the_evidence_is_about_to_be_overwritten(merge_refused,
                                                         monkeypatch):
    """Every stage writes its output with `>` to a fixed path, so the next 05:30
    round destroys it. A reader who does not know that loses the evidence by
    waiting a day — which is what happened on 08-25 and 09-07."""
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    assert "overwrites" in body, body
    assert "failed-rounds" in body, body


# --- a broken round is still refused -----------------------------------------

def test_a_missing_merge_record_is_still_raised_and_named(tmp_path, monkeypatch):
    """The stage record being absent must not become silence. It is a DIFFERENT
    state from a record that says which fork refused, and the alert has to say
    which one it is looking at — the whole 2026-09-08 confusion was a reader
    unable to tell "failed" from "never ran"."""
    state = tmp_path / "empty-state"
    state.mkdir()
    monkeypatch.setenv("GK_STATE_DIR", str(state))
    m = _mod()
    rc, summary, body = _raise(m, monkeypatch, "--tick-exit", "4")
    assert rc == m.RC_ESCALATED
    assert "daily_merge" in summary or "daily_merge" in body
    assert "does not exist" in body, body


def test_every_blocking_exit_code_is_named_not_just_the_one_that_fired(
        merge_refused, monkeypatch):
    """3 (source guards), 8 (fork self-checks) and 9 (vendored closure) reach the
    same refusal branch as 4. Fixing only the code that happened to fire today
    leaves the identical defect for the next stage that refuses."""
    m = _mod()
    for code, expected in ((3, "source guards"), (8, "fork self-checks"),
                           (9, "vendored closure")):
        rc, summary, body = _raise(m, monkeypatch, "--tick-exit", str(code))
        assert rc == m.RC_ESCALATED, code
        assert expected in body, (code, body)
        assert not summary.startswith("release failed"), (code, summary)


# --- controls: the paths that must not move ----------------------------------

def test_exit_five_is_still_reported_as_a_release_failure(tmp_path, monkeypatch):
    """CONTROL. Exit 5 IS a release failure — the build ran and died. Its wording
    and its record are unchanged, and a change that made every code read alike
    would break this."""
    state = tmp_path / "s5"
    state.mkdir()
    (state / "daily-release.json").write_text(
        json.dumps(_RELEASE_RECORD, indent=2) + "\n", encoding="utf-8")
    (state / "daily-release.txt").write_text("ERROR: failed to solve\n",
                                             encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(state))
    m = _mod()
    rc, summary, body = _raise(m, monkeypatch, "--tick-exit", "5")
    assert rc == m.RC_ESCALATED
    assert summary == ("release failed on the 2026-09-08 round "
                       "(run_tick exit 5)"), summary
    assert "registry_artefact_state_unmeasurable" in body, body
    assert "retryable" in body, body
    assert "failed-releases/" in body, body


def test_a_successful_round_still_raises_nothing(merge_refused, monkeypatch):
    """CONTROL. The value of this alarm is what it stays quiet about."""
    m = _mod()
    rc, summary, body = _raise(m, monkeypatch, "--tick-exit", "0")
    assert rc == m.RC_OK
    assert summary == "" and body == ""


def test_a_round_that_never_reached_a_release_still_raises_nothing(
        merge_refused, monkeypatch):
    m = _mod()
    rc, summary, _ = _raise(m, monkeypatch, "--tick-exit", "2",
                            "--tick-state", "not_checked_ai_norecord")
    assert rc == m.RC_OK
    assert summary == ""


def test_an_unreadable_tick_exit_is_still_norecord(merge_refused, monkeypatch):
    m = _mod()
    rc, _, _ = _raise(m, monkeypatch, "--tick-exit", "")
    assert rc == m.RC_NORECORD


# --- the table must not drift from the script it describes -------------------

def test_the_stage_table_covers_every_exit_code_run_tick_assigns():
    """DERIVED FROM THE TREE, not hand-listed. `run_tick.sh` is the only place
    that decides which stage owns which exit code; a new stage added there with
    no entry here silently reinstates the defect for that stage."""
    # getattr, not attribute access: the pre-fix module has no table at all, and
    # a control that dies on AttributeError proves only that the name is new. It
    # must run and answer "the table names none of them".
    stages = getattr(_mod(), "_STAGES", {})
    tick = (HERE / "run_tick.sh").read_text(encoding="utf-8")
    assigned = {}
    for var, code in re.findall(
            r'^\[ "\$\{([a-z_]+_rc)(?::-0)?\}" != "0" \] && \[ "\$\{rc\}" = "0" \]'
            r' && rc=(\d+)', tick, re.M):
        assigned[int(code)] = var
    assert assigned, "no rc= assignments found in run_tick.sh — the scan is broken"
    missing = sorted(set(assigned) - set(stages))
    assert not missing, (f"run_tick.sh assigns exit(s) {missing} that "
                         f"check_release_failed does not name: "
                         f"{[assigned[c] for c in missing]}")
    for code, var in assigned.items():
        assert stages[code]["rc_var"] == var, (
            f"exit {code} is {var} in run_tick.sh but "
            f"{stages[code]['rc_var']} here")


def test_every_named_evidence_file_is_one_run_tick_actually_writes():
    """A table naming a file the round does not write reads exactly like a table
    naming the right one, and quotes an absence forever."""
    stages = getattr(_mod(), "_STAGES", {})
    assert stages, "check_release_failed names no stage for any exit code"
    tick = (HERE / "run_tick.sh").read_text(encoding="utf-8")
    written = set(re.findall(r'\$\{LOG_DIR\}/([A-Za-z0-9._-]+)', tick))
    for code, spec in stages.items():
        assert spec["txt"] in written, (code, spec["txt"])
        if spec.get("json"):
            assert spec["json"] in written, (code, spec["json"])


def test_the_refusal_branch_retains_the_evidence_it_names():
    """The alert points at `failed-rounds/`; the round must actually write it.
    A promise in a body no program keeps is worse than no promise."""
    tick = (HERE / "run_tick.sh").read_text(encoding="utf-8")
    blocked = tick.split('if [ "${blocking_rc}" != "0" ]; then', 1)[1]
    blocked = blocked.split("\nelif ", 1)[0]
    assert "archive_round_evidence.py" in blocked, blocked[:800]
    assert "daily-merge.txt" in blocked and "daily-merge.json" in blocked


# --- the refusal is recorded positively, not as an absence -------------------

def test_the_alert_names_every_stage_that_refused_not_only_the_first(
        merge_refused, monkeypatch):
    """`run_tick`'s status chain is first-wins, so a round where `daily_merge`
    AND the fork self-checks both refused exits 4 and the code says nothing about
    8. `round-blocked.json` records all four, and the alert has to read it — the
    exit code alone under-reports a multi-stage refusal."""
    import record_blocked_round as REC
    (merge_refused / REC.RECORD_NAME).write_text(
        json.dumps(REC.record({"merge_rc": 1, "selftest_rc": 2},
                              round_at=ROUND_AT), indent=2) + "\n",
        encoding="utf-8")
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    assert "daily_merge" in body, body
    assert "fork self-checks" in body, body
    assert "selftest_rc" in body, body


def test_a_round_with_no_blocked_record_still_reports_the_stage(merge_refused,
                                                               monkeypatch):
    """CONTROL for the above. The record is new, so a round from before it
    existed — or one whose write failed — must still produce the stage report the
    exit code alone can give. A reader gets less, never nothing."""
    m = _mod()
    rc, _, body = _raise(m, monkeypatch, "--tick-exit", "4")
    assert rc == m.RC_ESCALATED
    assert "daily_merge" in body, body


def test_the_recorder_names_the_same_stages_run_tick_gates_on():
    """DERIVED FROM THE TREE. `blocking_rc` is set by exactly four rc variables
    in `run_tick.sh`; a fifth added there with no entry in the recorder would be a
    stage that can block a release and never appear in the record of why."""
    import record_blocked_round as REC
    tick = (HERE / "run_tick.sh").read_text(encoding="utf-8")
    gated = set(re.findall(
        r'^\[ "\$\{([a-z_]+_rc)\}" != "0" \] && blocking_rc=1', tick, re.M))
    assert gated, "no blocking_rc gates found in run_tick.sh — the scan is broken"
    named = {var for _, var, _ in REC.BLOCKING_STAGES}
    assert gated == named, (f"run_tick gates on {sorted(gated)}; the recorder "
                            f"names {sorted(named)}")


def test_the_refusal_branch_records_the_refusal_positively():
    tick = (HERE / "run_tick.sh").read_text(encoding="utf-8")
    blocked = tick.split('if [ "${blocking_rc}" != "0" ]; then', 1)[1]
    blocked = blocked.split("\nelif ", 1)[0]
    assert "record_blocked_round.py" in blocked, blocked[:600]


# --- the quoted evidence is the finding, not the whole file -------------------

def test_the_quoted_output_is_the_finding_not_a_wall_of_clean_rows(tmp_path,
                                                                   monkeypatch):
    """Measured on a real 38-fork `daily-merge.txt`: quoting the head printed 37
    ALREADY_CURRENT rows above the one row that mattered. An alert nobody reads
    to the end is the same defect as an alert that says nothing."""
    state = tmp_path / "wall"
    state.mkdir()
    rows = ["daily_merge: 38 fork(s), 0 merged, 1 need a human"]
    rows += [f"  fork{i:<10} ALREADY_CURRENT        level with upstream/master"
             for i in range(37)]
    rows.append("  Trilinos       UPSTREAM_DEFAULT_UNMEASURABLE  live upstream "
                "HEAD did not resolve to exactly one branch")
    (state / "daily-merge.txt").write_text("\n".join(rows) + "\n",
                                           encoding="utf-8")
    monkeypatch.setenv("GK_STATE_DIR", str(state))
    _, _, body = _raise(_mod(), monkeypatch, "--tick-exit", "4")
    quoted = body.split("## What daily_merge said")[1].split("```")[1]
    assert "UPSTREAM_DEFAULT_UNMEASURABLE" in quoted, quoted
    assert "ALREADY_CURRENT" not in quoted, quoted


def test_the_alert_and_the_log_agree_on_what_a_finding_is():
    """The rule lives in `merge_findings.sh` and the alert delegates to it. They
    are different files and a rename on one side alone is how the log and the
    alert start disagreeing about which forks refused."""
    m = _mod()
    awk = (HERE / "merge_findings.sh").read_text(encoding="utf-8")
    for state in m._MERGE_CLEAN:
        assert f'"{state}"' in awk, (
            f"{state} is clean to check_release_failed and not to "
            f"merge_findings.sh")
    named = set(re.findall(r'\$2 != "([A-Z_]+)"', awk))
    assert named == set(m._MERGE_CLEAN), (named, m._MERGE_CLEAN)


def test_the_filter_the_stage_names_is_the_one_that_exists():
    """A `findings` script that is missing or not executable silently degrades to
    quoting the head — which is correct behaviour and must not be the state we
    ship in."""
    import os as _os
    stages = getattr(_mod(), "_STAGES", {})
    assert stages, "check_release_failed names no stage for any exit code"
    named = [(c, sp) for c, sp in stages.items() if sp.get("findings")]
    assert named, ("no stage names a findings filter, so every alert quotes the "
                   "head of a stage's output")
    for code, spec in stages.items():
        if not spec.get("findings"):
            continue
        script = HERE / spec["findings"]
        assert script.is_file(), (code, script)
        assert _os.access(script, _os.X_OK), (code, script)
