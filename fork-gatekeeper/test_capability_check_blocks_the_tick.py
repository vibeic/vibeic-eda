#!/usr/bin/env python3
"""The capability sweep must be able to STOP the 05:30 round (vibeic-eda#88).

Until 2026-08-05 `run_tick.sh` invoked `check_no_capability_lost.py` with a
trailing `|| true`. It printed `LOST:` lines and the round carried on — a check
that looks like a gate in the log and is not one, which is worse than no gate:
the tick's exit code read clean while a command the base image provided had
stopped resolving in ours.

The correct shape already existed one layer over, in `capability_gate.py`:
rc!=0 emits `built_red` and nothing is promoted, and **rc=2 (the image could not
be probed) blocks exactly like rc=1**, because measuring nothing proves nothing.

THIS TEST EXECUTES THE REAL SCRIPT. It does not read the source and assert that
a `|| true` is absent — that would pass against a `cap_rc` nobody folds into the
exit code, which is the same "correct check that nothing invokes" defect one
level up. The tick is run four times, end to end, with the capability program
stubbed to each interesting exit status, and the assertion is on `run_tick.sh`'s
OWN exit code.

Everything else the tick's exit code depends on is stubbed GREEN, so a 7 can
only have come from the capability block:
  guard_rc -> 3, merge_rc -> 4, release_rc -> 5, ship_rc -> 6, cap_rc -> 7,
  selftest_rc -> 8 (vibe-ic#813).
The `test_a_green_tick_exits_zero` case is what proves that stubbing worked;
without it every other assertion here would also pass against a tick that
exits non-zero for some unrelated reason.
"""
from __future__ import annotations

import os
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TICK = HERE / "run_tick.sh"

OK = "#!/usr/bin/env python3\nimport sys\nprint('stub ok')\nsys.exit(0)\n"
CLOSURE_OK = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
path = Path(sys.argv[sys.argv.index("--json") + 1])
path.write_text(json.dumps({
    "program": "vendored_dependency_closure",
    "status": "complete", "dry_run": False,
    "passes": [], "updated_hosts": [], "source_errors": [],
}))
print("closure stub ok")
'''


def _stub(path: Path, body: str, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def _fleet(tmp_path: Path, cap: str | None) -> tuple[Path, dict]:
    """A tree the real run_tick.sh can run in, with `cap` as the capability program."""
    home = tmp_path / "home"
    root = tmp_path / "eda"
    dirp = root / "fork-gatekeeper"
    dirp.mkdir(parents=True)

    shutil.copy2(TICK, dirp / "run_tick.sh")
    shutil.copy2(HERE / "check_ai_release_authorization.py",
                 dirp / "check_ai_release_authorization.py")
    (dirp / "FORKS.json").write_text(json.dumps({
        "org": "vibeic", "forks": [{"tool": "Demo"}],
    }))
    (root / "RELEASED.json").write_text('{"version": "0.0.1-test"}\n')

    # Everything whose status the tick folds into its own exit code.
    for name in ("check_fork_only.py", "check_pins_agree.py",
                 "check_dockerfile_layer_budget.py", "check_doc_counts.py",
                 "check_fork_presence_claims.py"):
        _stub(root / "tools" / name, OK)
    for name in ("daily_merge.py", "daily_release.py", "check_our_commits_ship.py",
                 "gatekeeper.py", "check_fork_selftests.py"):
        _stub(dirp / name, OK)
    _stub(dirp / "vendored_dependency_closure.py", CLOSURE_OK)
    if cap is not None:
        _stub(dirp / "check_no_capability_lost.py", cap)

    # run_tick.sh pins PATH to "${HOME}/.local/bin:...:/usr/bin:/bin", so a stub
    # `gh` goes there. Without a token the script exits 2 before anything runs.
    _stub(home / ".local" / "bin" / "gh",
          "#!/usr/bin/env bash\n[ \"$1\" = auth ] && echo gho_stubtoken\nexit 0\n")

    env = dict(os.environ)
    env.update(HOME=str(home), GK_STATE_DIR=str(tmp_path / "state"))
    return dirp / "run_tick.sh", env


def _authorized_tick(script: Path, env: dict):
    state = Path(env["GK_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    boundary = datetime.now(timezone.utc) - timedelta(seconds=2)
    started = boundary + timedelta(seconds=1)
    run_id = "a" * 64
    round_json = state / "daily_0530.json"
    round_json.write_text(json.dumps({
        "Demo": {
            "main": "main", "conflicted": [],
            "upstream_merge_outcome": "natural_success",
        },
        "_ai_decisions": {
            "cases": 0, "invoked": False, "cleared": True,
            "verified": True,
        },
        "_round": {
            "schema_version": 1, "run_id": run_id,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "applied": True,
        },
    }))
    capability = "e" * 64
    args = [
        "--ai-round-json", str(round_json),
        "--ai-forks-json", str(script.parent / "FORKS.json"),
        "--ai-round-at", boundary.isoformat(),
        "--ai-run-id", run_id,
        "--ai-capability", capability,
    ]
    (state / f"ai-release-handoff.{capability}.json").write_text(json.dumps({
        "schema_version": 1, "capability": capability,
        "parent_pid": os.getpid(), "round_json": str(round_json),
        "forks_json": str(script.parent / "FORKS.json"),
        "round_at": boundary.isoformat(), "run_id": run_id,
    }))
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True,
        text=True, timeout=900)


def _run(tmp_path, cap):
    script, env = _fleet(tmp_path, cap)
    p = _authorized_tick(script, env)
    return p


def test_a_green_tick_exits_zero(tmp_path):
    """The control. Without this, every assertion below is satisfied by a tick
    that fails for an unrelated reason, and the stubbing is unverified."""
    p = _run(tmp_path, OK)
    assert p.returncode == 0, f"stubbed-green tick exited {p.returncode}\n{p.stdout[-3000:]}"


def test_zero_closure_exit_without_terminal_evidence_blocks_release(tmp_path):
    script, env = _fleet(tmp_path, OK)
    _stub(script.parent / "vendored_dependency_closure.py", OK)
    p = _authorized_tick(script, env)
    assert p.returncode == 10, p.stdout[-3000:]
    assert "zero exit lacked an exact complete sidecar" in p.stdout


@pytest.mark.parametrize("closure_rc,terminal", [(0, True), (1, True), (2, True), (0, False)])
def test_closure_result_keeps_ai_authority_and_reaches_alert_consumer(
        tmp_path, monkeypatch, closure_rc, terminal):
    """Run the real producer, outer wrapper and alert decision in sequence.

    External work is stubbed; no image is built and the notifier cannot publish.
    An authorized tick blocked on closure must retain its distinct failure,
    while a complete closure remains quiet. Real AI refusal stays covered by
    test_ai_release_authorization_gate's separate exit-9 control.
    """
    import check_release_failed as alert
    from test_ai_release_authorization_gate import _shell_fleet

    script, env = _fleet(tmp_path / "producer", OK)
    if terminal:
        status = "complete" if closure_rc == 0 else "needs_human" if closure_rc == 1 else "unmeasured"
        body = CLOSURE_OK.replace('"status": "complete"', f'"status": "{status}"')
        body += f"\nraise SystemExit({closure_rc})\n"
    else:
        body = OK
    _stub(script.parent / "vendored_dependency_closure.py", body)
    _stub(script.parent / "daily_release.py", '''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["GK_STATE_DIR"], "candidate-constructed").write_text("called")
''')
    produced = _authorized_tick(script, env)
    blocked = closure_rc != 0 or not terminal
    assert (Path(env["GK_STATE_DIR"]) / "candidate-constructed").exists() is (not blocked)

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    wrapper, outer_env, state = _shell_fleet(consumer, "verified")
    _stub(wrapper.parent / "run_tick.sh", f"#!/usr/bin/env bash\nexit {produced.returncode}\n")
    result = subprocess.run(["bash", str(wrapper)], env=outer_env, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 1  # This fixture deliberately retains SIX=1.
    captured = json.loads((state / "ll-captured.json").read_text())
    assert captured["tick_state"] == "ran", captured
    assert captured["ai"]["status"] == "authorized"
    assert produced.returncode == (10 if blocked else 0)
    assert "forced NORECORD" not in (state / "daily_0530.log").read_text()

    escalations = []
    def notify(summary, body, **kwargs):
        escalations.append((summary, body, kwargs))
        return True, "captured at the external notification boundary"
    monkeypatch.setattr(alert, "_default_escalate", notify)
    alert_rc = alert.main(["--tick-exit", str(produced.returncode), "--tick-state",
                           captured["tick_state"], "--round-at", "2026-01-01T05:30:00+00:00",
                           "--release-json", str(state / "no-release.json")])
    assert alert_rc == (alert.RC_ESCALATED if blocked else alert.RC_OK)
    assert len(escalations) == int(blocked)


def test_a_lost_capability_stops_the_round(tmp_path):
    """rc=1 — something the base provided no longer resolves in our image."""
    p = _run(tmp_path, "#!/usr/bin/env python3\nimport sys\n"
                       "print('LOST: sby')\nsys.exit(1)\n")
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — the round was not stopped\n{p.stdout[-3000:]}"
    assert "LOST: sby" in p.stdout


def test_an_unprobeable_image_stops_the_round_exactly_like_a_loss(tmp_path):
    """rc=2 is 'nothing was compared'. It must NOT be the quiet one — that is
    precisely how a gate stays green for months without ever having run."""
    p = _run(tmp_path, "#!/usr/bin/env python3\nimport sys\n"
                       "print('could not probe the image')\nsys.exit(2)\n")
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — rc=2 was treated as a pass\n{p.stdout[-3000:]}"


def test_a_missing_capability_program_stops_the_round(tmp_path):
    """A checker that is not there checked nothing. The tick already says so in
    its log; before #88 it said so and exited 0 anyway."""
    p = _run(tmp_path, None)
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — a missing checker passed\n{p.stdout[-3000:]}"
    assert "nothing was checked" in p.stdout


def test_the_call_site_captures_the_status_on_the_same_line(tmp_path):
    """`python3 ... | head` then `rc=$?` reads the status of the WRONG process.

    build_and_regress.sh's first draft shipped that bug and this file's own
    `[ship]` block spells the rule out, so the shape is pinned rather than
    left to be rediscovered.
    """
    src = TICK.read_text()
    i = src.index('CAP_OUT="')
    block = src[i:i + 1800]
    code = "\n".join(ln for ln in block.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "cap_rc=$?" in code
    assert "capability-lost.json" in code
    # The `|| true` this issue is about must not come back on the CHECK itself.
    # The `grep ... || true` on the following line is fine and stays: grep exits
    # 1 when it matches nothing, which is the clean case.
    for ln in code.splitlines():
        if "check_no_capability_lost.py" in ln or "capability-lost.json" in ln:
            assert "|| true" not in ln, \
                "the capability check is back to report-only (vibeic-eda#88)"


def test_the_tick_still_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0


def test_old_or_released_tag_audit_is_not_mislabeled_candidate_authority():
    """run_tick can only name REACH_IMG from RELEASED.json. Before a release
    that is yesterday's image; after it, it is an audit of what was published.
    The exact candidate authority therefore has to live in daily_release, where
    the candidate tag exists, and its smoke must precede docker push."""
    tick = TICK.read_text(encoding="utf-8")
    release_call = tick.index('python3 "${DIR}/daily_release.py"')
    released_audit = tick.index('CAP_OUT="${LOG_DIR}/capability-lost.txt"')
    assert release_call < released_audit, (
        "check_no_capability_lost was moved before candidate construction and "
        "is being mistaken for evidence about an image that does not exist yet")

    release = (HERE / "daily_release.py").read_text(encoding="utf-8")
    smoke = release.index("bad = smoke_image(tag)")
    candidate_push = release.index('["docker", "push", tag]', smoke)
    assert smoke < candidate_push, (
        "daily_release can push the exact candidate before its capability "
        "smoke authorizes it")


# --- the program itself must be able to fail, or the wiring above is decorative

def _cap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cap_lost", HERE / "check_no_capability_lost.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_probe_that_did_not_run_is_not_an_empty_loss_list():
    """The bug that made #88's wiring worth nothing on its own.

    `unresolvable` discarded `docker run`'s exit status, so an image that could
    not be started produced empty output, empty output meant "every name
    resolved", and the program printed `[PASS]` and exited 0. Measured against a
    tag that does not exist:

        before   0 no longer resolve ... [PASS]   rc=0
        after    [NOT CHECKED] could not probe ... rc=2

    Making the CALL SITE fail-closed while the program itself could not fail
    would have moved the defect, not fixed it.
    """
    C = _cap()
    C._sh = lambda *a, **k: (125, "", "Unable to find image")
    assert C.unresolvable("nope:nope", ["yosys", "klayout"]) is None, \
        "a failed probe still reads as 'nothing was lost'"


def test_a_probe_that_ran_still_reports_normally():
    """…and the other direction, or the fix is just 'always return None'."""
    C = _cap()
    C._sh = lambda *a, **k: (0, "klayout\n", "")
    assert C.unresolvable("img", ["yosys", "klayout"]) == ["klayout"]


def test_the_unprobeable_case_exits_two_and_says_nothing_was_compared(capsys):
    C = _cap()
    C.base_image = lambda df: "base:1"
    C.replaced_prefixes = lambda df: ["yosys"]
    C.command_names = lambda img, pre: ["yosys", "klayout"]
    C.unresolvable = lambda img, names: None
    rc = C.main([ "img:1", "--dockerfile", str(HERE.parent / "Dockerfile")])
    assert rc == C.RC_NOTHING
    assert "NOT CHECKED" in capsys.readouterr().err


def test_the_stale_json_is_overwritten_when_the_probe_fails(tmp_path):
    """Returning before the write leaves YESTERDAY's `"lost": []` on disk, and a
    consumer reading it sees a pass nothing produced today."""
    C = _cap()
    j = tmp_path / "cap.json"
    j.write_text('{"program": "check_no_capability_lost", "lost": []}\n')
    C.base_image = lambda df: "base:1"
    C.replaced_prefixes = lambda df: ["yosys"]
    C.command_names = lambda img, pre: ["yosys"]
    C.unresolvable = lambda img, names: None
    C.main(["img:1", "--dockerfile", str(HERE.parent / "Dockerfile"),
            "--json", str(j)])
    import json as _j
    got = _j.loads(j.read_text())
    assert got["lost"] is None and got.get("error"), got


def test_a_failed_base_listing_also_replaces_stale_green_json(tmp_path):
    """The first Docker arm is evidence too; failing there cannot keep yesterday."""
    C = _cap()
    j = tmp_path / "cap.json"
    j.write_text('{"program": "check_no_capability_lost", "lost": []}\n')
    C.base_image = lambda df: "base:1"
    C.replaced_prefixes = lambda df: ["yosys"]
    C.command_names = lambda img, pre: []
    rc = C.main(["img:1", "--dockerfile", str(HERE.parent / "Dockerfile"),
                 "--json", str(j)])
    import json as _j
    got = _j.loads(j.read_text())
    assert rc == C.RC_NOTHING
    assert got["status"] == "not_checked" and got["lost"] is None
    assert "list commands" in got["error"]
