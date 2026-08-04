"""A round that is not retained cannot be asked whether a defect ever fired.

vibeic-eda#85. `daily_0530.json` was written to ONE path and overwritten every
morning, so the only record of what a round decided was the record of what the
MOST RECENT round decided. Answering that question for #82 instead cost a
reconstruction from GitHub's Events API (~300-event cap; the busiest forks were
already down to a ~1-day window) and each clone's reflog (expires, and a
`git gc` can take it) — and 14 of 36 forks were not measurable that way at all.

THE FIRST TEST IN THIS FILE IS THE CONTROL, AND IT IS BEHAVIOURAL.
==================================================================
It drives the real entry point, `daily_0530.main_`, over two rounds of a real
git fleet on disk, and then asks the state directory what round ONE decided —
by walking files and parsing JSON, never by importing the module this change
adds. On a tree without that module the test still COLLECTS, still RUNS, and
fails because round one's verdict is gone; it does not fail because a name is
missing. A control that dies on `ImportError`/`AttributeError` against the old
tree has proved that the old tree lacks a symbol, which was never in doubt.

That is also why `round_record` is imported inside the test bodies below rather
than at module scope: a module-level import would make the whole file
uncollectable on the old tree and take the control down with it.

BOTH DIRECTIONS, EVERY TEST. A retention layer that recorded every fork as
"unconfirmed" would satisfy every "must be flagged" assertion here and be
useless; a `_prune` that deleted nothing would satisfy every "must survive"
assertion. Each test pins the case it is about AND the case next to it.
"""
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D                                     # noqa: E402


def _rr():
    """The module under test, imported LATE. See the file docstring."""
    import round_record                                     # noqa: PLC0415
    return round_record


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd)


def _commit(repo, msg):
    """A commit that CHANGES THE TREE. An empty one would leave the round's
    `same_content_divergence` step correctly deciding there is nothing of ours
    to publish, which is a different morning than the one being staged here."""
    (pathlib.Path(repo) / msg).write_text(msg)
    _run("git", "-C", str(repo), "add", msg)
    _run("git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", msg, cwd=str(repo))


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A forks root the round can actually walk: one clone, a bare `origin` it
    may push to, and a bare `upstream` that can be advanced between rounds."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run("git", "init", "-q", "-b", "master", str(seed))
    _commit(seed, "a")
    up = tmp_path / "up.git"
    origin = tmp_path / "origin.git"
    _run("git", "init", "-q", "--bare", str(up))
    _run("git", "init", "-q", "--bare", str(origin))
    _run("git", "-C", str(seed), "push", "-q", str(up), "master")
    _run("git", "-C", str(seed), "push", "-q", str(origin), "master")

    forks = tmp_path / "forks"
    forks.mkdir()
    clone = forks / "toolfork"
    _run("git", "clone", "-q", str(origin), str(clone))
    _run("git", "-C", str(clone), "remote", "add", "upstream", str(up))
    _run("git", "-C", str(clone), "fetch", "-q", "upstream")

    state = tmp_path / "state"
    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "EDA", tmp_path / "no-such-eda-checkout")
    monkeypatch.setenv("GK_STATE_DIR", str(state))
    return {"tmp": tmp_path, "seed": seed, "up": up, "origin": origin,
            "clone": clone, "forks": forks, "state": state,
            "g": ("git", "-C", str(clone))}


def _states_recorded_for(state_dir, fork):
    """Every upstream state `fork` is recorded in, read out of WHATEVER the
    round left behind — by walking the state directory and parsing JSON.

    Deliberately ignorant of this change's layout and of its module: a full
    report is one JSON object keyed by fork, an index is one JSON object per
    line, and both are read the same way. That is what lets this run against a
    tree that retains nothing and fail for the right reason.
    """
    seen = []
    if not pathlib.Path(state_dir).is_dir():
        return seen
    for p in sorted(pathlib.Path(state_dir).rglob("*")):
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        try:                                    # a whole-file report object
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            rep = obj.get(fork)
            if isinstance(rep, dict) and rep.get("upstream"):
                seen.append(rep["upstream"])
            continue
        for ln in text.splitlines():            # or one object per line
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("fork") == fork and row.get("state"):
                seen.append(row["state"])
    return seen


def _report(**forks):
    """A round report in the shape `main_` builds, from per-fork evidence."""
    out = {}
    for name, ev in forks.items():
        state = ev.pop("state")
        out[name] = {"main": "master", "needs_human": False, "pin": ev.pop("pin", None),
                     "upstream": state, "upstream_evidence": ev}
    return out


def _ev(state, confirmed_by, fetch="ok", **extra):
    return dict(state=state, confirmed_by=confirmed_by, fetch=fetch,
                ref="upstream/master", tip_before_fetch=None, tip_seen="a" * 40,
                remote_tip=None, behind=0, **extra)


# ── 0. THE CONTROL. Two rounds; the first one's verdict must survive the second ─
def test_the_first_rounds_verdict_survives_the_second_round(fleet):
    """#85 stated as behaviour, driven through the real entry point.

    Round one finds the fork current. Round two finds a commit and merges it,
    overwriting the single `--json` path. The question "what did round one
    decide about toolfork" must still have an answer afterwards.

    NOTHING FROM THE NEW MODULE IS NAMED HERE. On a tree that retains nothing
    this test runs to completion and fails on the missing verdict, which is the
    defect; it does not fail on a missing import, which is not.
    """
    live = fleet["tmp"] / "daily_0530.json"
    argv = ["--apply", "--no-ai", "--skip-build", "--json", str(live)]

    D.main_(argv)                                            # round one
    assert json.loads(live.read_text())["toolfork"]["upstream"] == "already current"

    _commit(fleet["seed"], "b")
    _run("git", "-C", str(fleet["seed"]), "push", "-q", str(fleet["up"]), "master")
    D.main_(argv)                                            # round two

    # The overwrite is REAL and is not what saves us: the live file now knows
    # only about round two.
    assert json.loads(live.read_text())["toolfork"]["upstream"] == \
        "merged 1 upstream commit(s)"

    states = _states_recorded_for(fleet["state"], "toolfork")
    assert "merged 1 upstream commit(s)" in states, states   # round two, retained
    assert "already current" in states, (                    # round one, THE DEFECT
        "round one's verdict is not recoverable from anything the round "
        f"retained; the state directory only knows {states}")


def test_the_control_would_notice_a_round_that_recorded_nothing(fleet):
    """The control's other direction: `_states_recorded_for` is not a function
    that returns a hit for everything. An empty state directory, and a fork the
    round never saw, both read as nothing recorded."""
    assert _states_recorded_for(fleet["state"], "toolfork") == []
    D.main_(["--apply", "--no-ai", "--skip-build"])
    assert _states_recorded_for(fleet["state"], "toolfork") != []
    assert _states_recorded_for(fleet["state"], "a-fork-that-does-not-exist") == []


# ── 1. a dated full report AND an append-only index ──────────────────────────
def test_write_makes_a_dated_report_and_a_second_round_appends_to_the_index(tmp_path):
    rr = _rr()
    state = tmp_path / "state"
    t1 = datetime(2026, 8, 5, 5, 30).astimezone()
    t2 = t1 + timedelta(days=1)

    r1 = rr.write(_report(toolfork=_ev("already current", "ls_remote")), state, when=t1)
    assert r1["error"] is None, r1
    full1 = state / "rounds" / r1["stamp"] / "daily_0530.json"
    assert full1.is_file(), sorted(p.name for p in (state / "rounds").iterdir())
    assert json.loads(full1.read_text())["toolfork"]["upstream"] == "already current"
    idx = state / "rounds" / "index.jsonl"
    before = idx.read_bytes()

    r2 = rr.write(_report(toolfork=_ev("merged 1 upstream commit(s)",
                                       "fetch_moved_ref")), state, when=t2)
    assert r2["error"] is None, r2
    assert r2["stamp"] != r1["stamp"]
    # APPENDED, not rewritten: round one's bytes are still the prefix of the file.
    assert idx.read_bytes().startswith(before)
    assert idx.read_bytes() != before
    # and round one's own report was not touched or replaced
    assert json.loads(full1.read_text())["toolfork"]["upstream"] == "already current"
    assert (state / "rounds" / r2["stamp"] / "daily_0530.json").is_file()


# ── 2. the coverage boundary is IN the record, once ──────────────────────────
def test_the_coverage_note_is_the_first_line_and_appears_exactly_once(tmp_path):
    rr = _rr()
    state = tmp_path / "state"
    t = datetime.now().astimezone()
    for i in range(3):
        rr.write(_report(toolfork=_ev("already current", "ls_remote")), state,
                 when=t + timedelta(days=i))
    lines = [json.loads(l) for l in
             (state / "rounds" / "index.jsonl").read_text().splitlines() if l.strip()]
    assert "note" in lines[0] and "fork" not in lines[0], lines[0]
    assert lines[0]["note"] == rr.COVERAGE_NOTE
    assert sum(1 for l in lines if "note" in l) == 1, lines
    # the other direction: every OTHER line is a fork row, not more prose
    assert all("fork" in l for l in lines[1:]), lines[1:]


# ── 3. THE ACCEPTANCE TEST: `confirmed_by` separates current from could-not-tell ─
def test_only_an_unconfirmed_currency_claim_is_a_hit(tmp_path):
    """A row claiming `already current` with `confirmed_by: null` is a round
    that could not tell, published as a round that could. That is #82's
    question, asked of the record instead of of GitHub's API and a reflog."""
    rr = _rr()
    state = tmp_path / "state"
    rr.write(_report(
        confirmed=_ev("already current", "ls_remote"),
        moved=_ev("merged 3 upstream commit(s)", "fetch_moved_ref"),
        unmeasured=_ev("already current", None),
        merged_unmeasured=_ev("merged 2 upstream commit(s)", None),
        # NOT a currency claim: it admits it does not know, so it is not a
        # round claiming something it could not confirm.
        honest=_ev("FETCH FAILED (rc=128) — upstream: state is UNKNOWN, not "
                   "current", None, fetch="failed"),
        no_remote=_ev("no upstream remote", None, fetch="no_upstream_remote"),
    ), state)
    hits = {h["fork"] for h in rr.unconfirmed_currency_claims(rr.read_index(state))}
    assert hits == {"unmeasured", "merged_unmeasured"}, hits
    # the constant those two spellings come from, pinned as literals
    assert set(rr._CURRENCY_CLAIMS) == {"already current", "merged"}


# ── 4. the CLI answers it, from the record alone ─────────────────────────────
def test_fired_exits_1_and_names_the_fork_only_when_a_bad_row_exists(tmp_path, capsys):
    rr = _rr()
    state = tmp_path / "state"
    good_day = datetime(2026, 8, 5, 5, 30).astimezone()
    bad_day = datetime(2026, 8, 6, 5, 30).astimezone()
    rr.write(_report(toolfork=_ev("already current", "ls_remote")), state, when=good_day)
    rr.write(_report(toolfork=_ev("already current", None)), state, when=bad_day)

    rc = rr.main(["--state-dir", str(state), "--fired", "--on", "2026-08-06"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "toolfork" in out and "2026-08-06" in out, out

    rc = rr.main(["--state-dir", str(state), "--fired", "--on", "2026-08-05"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 unconfirmed currency claims" in out, out


def test_a_window_with_no_rows_is_not_reported_as_nothing_fired(tmp_path, capsys):
    """#85 reappearing at the READ side. `--fired` over a window the record does
    not cover must not print the reassuring answer — that substitution is the
    whole defect, and here it would be made by the tool built to stop it."""
    rr = _rr()
    state = tmp_path / "state"
    rr.write(_report(toolfork=_ev("already current", "ls_remote")), state,
             when=datetime(2026, 8, 5, 5, 30).astimezone())

    rc = rr.main(["--state-dir", str(state), "--fired", "--on", "2026-07-01"])
    out = capsys.readouterr().out
    assert rc == 2, out                        # not 0, and not 1
    assert "NO ROWS RETAINED" in out, out
    assert "0 unconfirmed currency claims" not in out, out
    assert "no record was kept" in out, out

    # the other direction: a window the record DOES cover still answers 0, so
    # the guard above cannot be a blanket refusal to answer.
    rc = rr.main(["--state-dir", str(state), "--fired", "--on", "2026-08-05"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "NO ROWS RETAINED" not in out, out
    # and the count is of FORK rows, not of the coverage note beside them
    assert "across 1 retained fork row(s)" in out, out


# ── 5. the bound expires the bulk; the index is what answers "ever" ──────────
def test_prune_takes_the_full_report_and_never_the_index(tmp_path):
    rr = _rr()
    state = tmp_path / "state"
    now = datetime.now().astimezone()
    old = now - timedelta(days=500)
    r_old = rr.write(_report(toolfork=_ev("already current", None)), state,
                     when=old, keep_days=400)
    assert (state / "rounds" / r_old["stamp"] / "daily_0530.json").is_file()

    r_new = rr.write(_report(toolfork=_ev("already current", "ls_remote")), state,
                     when=now, keep_days=400)
    assert r_old["stamp"] in r_new["pruned"], r_new
    assert not (state / "rounds" / r_old["stamp"]).exists()
    assert (state / "rounds" / r_new["stamp"] / "daily_0530.json").is_file()

    # THE POINT OF THE SPLIT: the pruned round still answers the #82 question.
    rows = rr.read_index(state)
    hits = rr.unconfirmed_currency_claims(rows)
    assert [h["round"] for h in hits] == [r_old["stamp"]], hits
    assert any(r.get("round") == r_old["stamp"] for r in rows)


def test_keep_days_zero_prunes_nothing_and_a_foreign_directory_is_left_alone(tmp_path):
    rr = _rr()
    state = tmp_path / "state"
    now = datetime.now().astimezone()
    old = now - timedelta(days=500)

    r_old = rr.write(_report(toolfork=_ev("already current", None)), state,
                     when=old, keep_days=0)
    r_new = rr.write(_report(toolfork=_ev("already current", None)), state,
                     when=now, keep_days=0)
    assert r_new["pruned"] == []
    assert (state / "rounds" / r_old["stamp"]).is_dir()      # bound disabled

    # NOT one of ours — the name is not a stamp, so it is not the round's to
    # delete however old it is.
    foreign = state / "rounds" / "notes-from-a-human"
    foreign.mkdir()
    (foreign / "keep me").write_text("x")
    r_last = rr.write(_report(toolfork=_ev("already current", None)), state,
                      when=now, keep_days=400)
    assert r_old["stamp"] in r_last["pruned"], r_last     # ours, expired -> gone
    assert foreign.is_dir() and (foreign / "keep me").is_file()   # not ours -> kept


# ── 6. retention must never be the reason a morning does not run ─────────────
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_an_unwritable_state_dir_is_an_error_not_an_exception(tmp_path):
    rr = _rr()
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        out = rr.write(_report(toolfork=_ev("already current", None)), state)
    finally:
        state.chmod(0o700)
    assert out["error"], out
    assert "Error" in out["error"] or "error" in out["error"].lower(), out


def test_a_report_that_cannot_be_serialised_is_an_error_not_an_exception(tmp_path):
    """The failure this call INTRODUCES. `main_`'s `--json` write is optional;
    this one is not, so a report carrying something `json` cannot encode had a
    new path to killing the round."""
    rr = _rr()
    state = tmp_path / "state"
    report = _report(toolfork=_ev("already current", "ls_remote"))
    report["toolfork"]["pin"] = object()                     # not serialisable
    out = rr.write(report, state)
    assert out["error"], out
    assert "TypeError" in out["error"], out


def test_a_writable_state_dir_is_not_an_error(tmp_path):
    """The paired direction: a `write` that returned `error` unconditionally
    would satisfy both tests above and retain nothing at all."""
    rr = _rr()
    state = tmp_path / "state"
    out = rr.write(_report(toolfork=_ev("already current", "ls_remote")), state)
    assert out["error"] is None, out
    assert out["rows"] == 1, out
    assert (state / "rounds" / "index.jsonl").is_file()


# ── 7. an empty window is NO RECORD KEPT, not "clean" ────────────────────────
def test_describe_coverage_says_no_rounds_retained_rather_than_clean(tmp_path):
    rr = _rr()
    assert "NO ROUNDS RETAINED" in rr.describe_coverage([])
    assert "not the same as nothing having happened" in rr.describe_coverage([])

    state = tmp_path / "state"
    t = datetime.now().astimezone()
    a = rr.write(_report(toolfork=_ev("already current", "ls_remote")), state, when=t)
    b = rr.write(_report(toolfork=_ev("already current", "ls_remote")), state,
                 when=t + timedelta(days=1))
    got = rr.describe_coverage(rr.read_index(state))
    assert "NO ROUNDS RETAINED" not in got, got
    assert a["stamp"] in got and b["stamp"] in got, got
    assert rr.COVERAGE_NOTE in got, got       # the boundary travels with the answer


# ── 8. the pin comes from the module that owns the grammar ───────────────────
def test_image_pins_is_empty_rather_than_raising_when_there_is_nothing_to_read(tmp_path):
    assert D.image_pins(tmp_path / "no-such-checkout") == {}
    (tmp_path / "empty").mkdir()
    assert D.image_pins(tmp_path / "empty") == {}


def test_image_pins_finds_a_planted_pin_and_agrees_with_the_one_parser(tmp_path):
    """Both directions of the same fact: the pin is found, and it is the value
    `discover_forks.parse_dockerfile_pins` gives for the same text. A second
    parser here is how two programs come to disagree about the same pin."""
    import discover_forks
    sha = "0" * 39 + "a"
    text = ("ARG FOO_REPO=https://github.com/vibeic/foo.git\n"
            f"ARG FOO_REF={sha}  # branch master\n"
            'RUN git clone "${FOO_REPO}" /foo && git -C /foo checkout ${FOO_REF}\n')
    d = tmp_path / "eda" / "tools" / "foo"
    d.mkdir(parents=True)
    (d / "Dockerfile").write_text(text)

    got = D.image_pins(tmp_path / "eda")
    assert got == {"foo": sha}, got
    assert got["foo"] == discover_forks.parse_dockerfile_pins(text)["foo"]["ref"]
