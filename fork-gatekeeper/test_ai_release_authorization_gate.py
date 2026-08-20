#!/usr/bin/env python3
"""The 05:30 release rail requires verified AI delivery evidence."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "ai_release_authorization", HERE / "check_ai_release_authorization.py")
A = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(A)
sys.path.insert(0, str(HERE))
import capture_release_ll as LL  # noqa: E402


def _times() -> tuple[str, str, str]:
    boundary = datetime.now(timezone.utc) - timedelta(minutes=2)
    started = boundary + timedelta(seconds=1)
    finished = started + timedelta(seconds=1)
    return boundary.isoformat(), started.isoformat(), finished.isoformat()


def _record(kind: str) -> tuple[dict, str]:
    boundary, started, finished = _times()
    meta = {
        "schema_version": 1, "run_id": "a" * 64,
        "started_at": started, "finished_at": finished, "applied": True,
    }
    if kind == "malformed":
        return {"_round": meta}, boundary
    if kind == "no_cases":
        return {
            "Demo": {
                "main": "main", "conflicted": [],
                "upstream_merge_outcome": "natural_success",
            },
            "_ai_decisions": {
                "cases": 0, "invoked": False, "cleared": True,
                "verified": True,
            },
            "_round": meta,
        }, boundary

    verified = kind == "verified"
    case = {"branch": "origin/topic", "direction": "our branch -> our mainline"}
    proof = {
        "fork": "Demo", "branch": "origin/topic", "mainline": "main",
        "source_tip": "b" * 40, "published_tip": "c" * 40,
        "verified": verified,
        "reason": "source tip is carried" if verified else "source tip absent",
    }
    return {
        "Demo": {
            "main": "main", "conflicted": [case],
            "upstream_merge_outcome": "natural_success",
        },
        "_ai_decisions": {
            "cases": 1, "invoked": True, "rc": 0, "outcome": "natural",
            "verified": verified, "verified_cases": int(verified),
            "unverified_cases": [] if verified else [{
                "fork": "Demo", "branch": "origin/topic",
                "verification": "source tip absent",
            }],
            "verification": [proof],
        },
        "_round": meta,
    }, boundary


def _assess(record: dict, boundary: str):
    return A.assess(
        record, boundary, registered_forks=["Demo"], source="round.json",
        registry_source="FORKS.json", registry_sha256="d" * 64)


def _registry(path: Path, tools=("Demo",)) -> Path:
    path.write_text(json.dumps({
        "org": "vibeic", "forks": [{"tool": tool} for tool in tools]}))
    return path


@pytest.mark.parametrize("kind", ["verified", "no_cases"])
def test_verified_and_no_case_rounds_authorize(kind):
    record, boundary = _record(kind)
    rc, result = _assess(record, boundary)
    assert rc == A.RC_AUTHORIZED
    assert result["status"] == "authorized"
    assert result["unverified_count"] == 0


def test_rc_zero_cannot_authorize_an_unverified_postcondition():
    record, boundary = _record("unverified")
    assert record["_ai_decisions"]["rc"] == 0
    rc, result = _assess(record, boundary)
    assert rc == A.RC_BLOCKED
    assert result["status"] == "blocked"
    assert result["case_results"][0]["verified"] is False
    assert "published-mainline postcondition" in " ".join(result["reasons"])


def test_json_false_is_not_integer_zero_in_checker_or_ll(tmp_path):
    record, boundary = _record("verified")
    record["_ai_decisions"]["rc"] = False
    registry = _registry(tmp_path / "FORKS.json")
    rc, result = A.assess(
        record, boundary, registered_forks=["Demo"], source="round.json",
        registry_source=str(registry),
        registry_sha256=hashlib.sha256(registry.read_bytes()).hexdigest())
    assert rc == A.RC_BLOCKED
    assert result["status"] == "blocked"
    assert result["transport"]["rc"] is False
    assert "not integer 0" in " ".join(result["reasons"])

    # Defence in depth: even a forged `authorized` label on that normalized
    # record cannot become a clean LL input.
    result["status"] = "authorized"
    result["reasons"] = []
    result["source_errors"] = []
    accepted, errors = LL._validate_ai_authorization(
        result, tmp_path / "ai.json", boundary, registry)
    assert accepted is None
    assert any("AI transport" in error for error in errors)


def test_conflict_denominator_cannot_be_hidden_by_cases_zero():
    record, boundary = _record("verified")
    record["_ai_decisions"].update(
        cases=0, verified=True, verified_cases=0,
        verification=[], unverified_cases=[])
    rc, result = _assess(record, boundary)
    assert rc == A.RC_NORECORD
    assert result["status"] == "norecord"
    assert any("round contains 1 conflicted" in error
               for error in result["source_errors"])


def test_daily_rows_must_exactly_cover_the_registered_fleet():
    record, boundary = _record("verified")
    rc, result = A.assess(
        record, boundary, registered_forks=["Demo", "Missing"],
        registry_source="FORKS.json", registry_sha256="d" * 64)
    assert rc == A.RC_NORECORD
    assert any("missing top-level row(s): Missing" in error
               for error in result["source_errors"])

    record, boundary = _record("verified")
    record["Extra"] = {"main": "main", "conflicted": []}
    rc, result = _assess(record, boundary)
    assert rc == A.RC_NORECORD
    assert any("unexpected top-level row(s): Extra" in error
               for error in result["source_errors"])


def test_malformed_daily_row_cannot_stand_for_an_empty_conflict_set():
    record, boundary = _record("no_cases")
    record["Demo"] = {"error": "checkout failed"}
    rc, result = _assess(record, boundary)
    assert rc == A.RC_NORECORD
    assert any("Demo.main is missing or malformed" in error
               for error in result["source_errors"])
    assert any("Demo.conflicted is missing or not a list" in error
               for error in result["source_errors"])


@pytest.mark.parametrize("incomplete", [
    {"error": "checkout failed"},
    {"ours_skipped": ""},
    {"ours_skipped": "BRANCH LIST UNKNOWN — origin fetch failed",
     "merged": []},
    {"merge_unmeasurable": []},
    {"merge_unmeasurable": [{"branch": "origin/topic",
                               "outcome": "stalled"}]},
    {"upstream_merge_outcome": "unmeasurable"},
])
def test_incomplete_conflict_discovery_cannot_authorize_zero_cases(incomplete):
    record, boundary = _record("no_cases")
    record["Demo"].update(incomplete)

    rc, result = _assess(record, boundary)

    assert rc == A.RC_NORECORD
    assert result["status"] == "norecord"
    assert any("conflict discovery" in error
               for error in result["source_errors"])


def test_verified_upstream_conflict_still_waits_for_branch_discovery_round():
    record, boundary = _record("verified")
    record["Demo"].update(
        upstream_merge_outcome="conflict",
        ours_skipped=("upstream landing did not form a verified successful "
                      "record; branch consolidation waits"),
    )

    rc, result = _assess(record, boundary)

    assert rc == A.RC_NORECORD
    assert result["status"] == "norecord"
    assert result["verified_count"] == 1
    assert any("ours_skipped" in error or "upstream_merge_outcome" in error
               for error in result["source_errors"])


@pytest.mark.parametrize(
    ("registry_value", "expected"), [
        ({"forks": [{"tool": "Demo"}, {"tool": "demo"}]},
         "duplicate tool"),
        ({"forks": [{"tool": " bad "}]}, "tool is malformed"),
        ({"forks": "Demo"}, "has no forks list"),
    ])
def test_duplicate_or_malformed_registry_is_norecord(
        tmp_path, registry_value, expected):
    record, boundary = _record("verified")
    round_json = tmp_path / "round.json"
    round_json.write_text(json.dumps(record))
    registry = tmp_path / "FORKS.json"
    registry.write_text(json.dumps(registry_value))
    output = tmp_path / "authorization.json"
    rc = A.main([
        "--round-json", str(round_json), "--forks-json", str(registry),
        "--round-at", boundary, "--json", str(output),
    ])
    result = json.loads(output.read_text())
    assert rc == A.RC_NORECORD
    assert result["status"] == "norecord"
    assert any(expected in error for error in result["source_errors"])


def test_duplicate_daily_top_level_row_is_norecord(tmp_path):
    record, boundary = _record("verified")
    encoded = json.dumps(record)
    # A dict cannot represent this malformed wire input; append the duplicate
    # before parsing to prove the strict loader rejects ambiguous row coverage.
    encoded = encoded[:-1] + ', "Demo": ' + json.dumps(record["Demo"]) + "}"
    round_json = tmp_path / "round.json"
    round_json.write_text(encoded)
    registry = _registry(tmp_path / "FORKS.json")
    output = tmp_path / "authorization.json"
    rc = A.main([
        "--round-json", str(round_json), "--forks-json", str(registry),
        "--round-at", boundary, "--json", str(output),
    ])
    result = json.loads(output.read_text())
    assert rc == A.RC_NORECORD
    assert any("duplicate JSON key 'Demo'" in error
               for error in result["source_errors"])


@pytest.mark.parametrize("mutation", ["forks", "count", "hash"])
def test_ll_independently_rejects_a_forged_registry_identity(
        tmp_path, mutation):
    record, boundary = _record("verified")
    registry = _registry(tmp_path / "FORKS.json")
    rc, normalized = A.assess(
        record, boundary, registered_forks=["Demo"], source="round.json",
        registry_source=str(registry),
        registry_sha256=hashlib.sha256(registry.read_bytes()).hexdigest())
    assert rc == A.RC_AUTHORIZED
    forged = copy.deepcopy(normalized)
    if mutation == "forks":
        forged["registered_forks"] = []
    elif mutation == "count":
        forged["registered_count"] = 0
    else:
        forged["registry"]["sha256"] = "0" * 64
    accepted, errors = LL._validate_ai_authorization(
        forged, tmp_path / "ai.json", boundary, registry)
    assert accepted is None
    assert any("canonical fleet" in error or "registry.sha256" in error
               for error in errors)


def test_missing_input_replaces_any_old_authorization_with_norecord(tmp_path):
    output = tmp_path / "authorization.json"
    output.write_text('{"status":"authorized"}\n')
    registry = _registry(tmp_path / "FORKS.json")
    rc = A.main([
        "--round-json", str(tmp_path / "missing.json"),
        "--forks-json", str(registry),
        "--round-at", datetime.now(timezone.utc).isoformat(),
        "--json", str(output),
    ])
    result = json.loads(output.read_text())
    assert rc == A.RC_NORECORD
    assert result["status"] == "norecord"
    assert "missing" in result["source_errors"][0]


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _shell_fleet(tmp_path: Path, mode: str) -> tuple[Path, dict, Path]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    gate = tmp_path / "fork-gatekeeper"
    gate.mkdir()
    shutil.copy2(HERE / "run_0530.sh", gate / "run_0530.sh")
    shutil.copy2(
        HERE / "check_ai_release_authorization.py",
        gate / "check_ai_release_authorization.py")
    _registry(gate / "FORKS.json")

    _stub(gate / "daily_0530.py", r'''#!/usr/bin/env python3
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--apply",action="store_true")
p.add_argument("--ai-stall-grace"); p.add_argument("--json"); a=p.parse_args()
mode=os.environ["AI_FIXTURE"]
if mode == "missing": sys.exit(1)
now=datetime.now(timezone.utc); started=now
meta={"schema_version":1,"run_id":"a"*64,"started_at":started.isoformat(),
      "finished_at":now.isoformat(),"applied":True}
if mode == "malformed": record={"_round":meta}
elif mode == "no_cases":
 record={"Demo":{"main":"main","conflicted":[],"upstream_merge_outcome":"natural_success"},"_round":meta,
         "_ai_decisions":{"cases":0,"invoked":False,"cleared":True,"verified":True}}
else:
 ok=mode in {"verified","rc_false"}; case={"branch":"origin/topic","direction":"our branch -> our mainline"}
 proof={"fork":"Demo","branch":"origin/topic","mainline":"main","source_tip":"b"*40,
        "published_tip":"c"*40,"verified":ok,"reason":"published" if ok else "absent"}
 record={"Demo":{"main":"main","conflicted":[case],"upstream_merge_outcome":"natural_success"},"_round":meta,
  "_ai_decisions":{"cases":1,"invoked":True,"rc":False if mode == "rc_false" else 0,"outcome":"natural",
    "verified":ok,"verified_cases":int(ok),"verification":[proof],
    "unverified_cases":[] if ok else [{"fork":"Demo","branch":"origin/topic","verification":"absent"}]}}
Path(a.json).write_text(json.dumps(record))
# Deliberately 1 even when verified: raw SIX is not the release authorization.
sys.exit(1)
''')
    _stub(gate / "check_round_code_is_current.py",
          "#!/usr/bin/env python3\nraise SystemExit(0)\n")
    _stub(gate / "semantic_pytest_gate.py", r'''#!/usr/bin/env python3
import json, sys
from datetime import datetime, timezone
from pathlib import Path
record={"mechanism":"validated_semantic_progress_lease","exit_code":0,
 "outcome":"natural","started_at":datetime.now(timezone.utc).isoformat()}
for i,arg in enumerate(sys.argv[:-1]):
 if arg == "--json":
  path=Path(sys.argv[i+1]); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(record))
raise SystemExit(0)
''')
    _stub(gate / "run_tick.sh", r'''#!/usr/bin/env bash
echo called > "${GK_STATE_DIR}/run-tick.called"
exit 0
''')
    for name in ("discover_forks.py", "build_page.py"):
        _stub(gate / name, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
    _stub(gate / "fork_gap_report.py", r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
path=Path(sys.argv[sys.argv.index("--json")+1])
path.write_text(json.dumps({"q1_sync_lag_at_merge":0,"q1_sync_lag":0,
 "q2_our_commits_stranded":0,"q2_ours_past_the_pin_substantive":0}))
''')
    _stub(gate / "fork_github_convergence.py", r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path(sys.argv[sys.argv.index("--json")+1]).write_text("{}")
''')
    _stub(gate / "capture_release_ll.py", r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
ai=Path(sys.argv[sys.argv.index("--ai-authorization-json")+1])
state=Path(os.environ["GK_STATE_DIR"])
state.joinpath("ll-captured.json").write_text(json.dumps({
 "tick_state":sys.argv[sys.argv.index("--tick-state")+1],
 "ai":json.loads(ai.read_text())}))
''')
    _stub(gate / "check_ledger_is_fresh.py",
          "#!/usr/bin/env python3\nraise SystemExit(0)\n")
    env = dict(os.environ)
    env.update(HOME=str(home), GK_STATE_DIR=str(state), AI_FIXTURE=mode)
    return gate / "run_0530.sh", env, state


@pytest.mark.parametrize(
    "mode", ["unverified", "rc_false", "malformed", "missing"])
def test_real_0530_script_never_invokes_release_tick_without_ai_authority(
        tmp_path, mode):
    script, env, state = _shell_fleet(tmp_path, mode)
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True,
        timeout=30)
    assert result.returncode == 1
    assert not (state / "run-tick.called").exists()
    captured = json.loads((state / "ll-captured.json").read_text())
    assert captured["tick_state"] == (
        "not_checked_ai_unverified" if mode in {"unverified", "rc_false"}
        else "not_checked_ai_norecord")
    assert captured["ai"]["status"] == (
        "blocked" if mode in {"unverified", "rc_false"} else "norecord")


@pytest.mark.parametrize("mode", ["verified", "no_cases"])
def test_real_0530_script_authorizes_verified_or_empty_cases_even_when_six_is_one(
        tmp_path, mode):
    script, env, state = _shell_fleet(tmp_path, mode)
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True,
        timeout=30)
    # SIX remains nonzero and therefore the overall round is honestly red, but
    # the narrower release authorization is green and the tick was invoked.
    assert result.returncode == 1
    assert (state / "run-tick.called").is_file()
    captured = json.loads((state / "ll-captured.json").read_text())
    assert captured["tick_state"] == "ran"
    assert captured["ai"]["status"] == "authorized"


def test_shell_and_python_entrypoints_parse():
    source = (HERE / "run_0530.sh").read_text()
    checker = source[source.index("check_ai_release_authorization.py"):
                     source.index("AI_AUTH=$?", source.index(
                         "check_ai_release_authorization.py"))]
    assert '--forks-json "${DIR}/FORKS.json"' in checker
    assert subprocess.run(["bash", "-n", str(HERE / "run_0530.sh")]).returncode == 0
    assert subprocess.run([
        sys.executable, "-m", "py_compile",
        str(HERE / "check_ai_release_authorization.py"),
        str(HERE / "capture_release_ll.py"),
    ]).returncode == 0


# The wrapper tests above prove its branch ordering.  These execute the other
# release-capable entrypoint itself: a direct/stale run_tick invocation must not
# be able to bypass that ordering just because its own source/merge checks pass.
_DIRECT_OK_STUB = (
    "#!/usr/bin/env python3\nimport sys\nprint('stub ok')\nsys.exit(0)\n")
_DIRECT_RELEASE_STUB = r'''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["RELEASE_MARKER"]).write_text("invoked\n")
print("release stub ok")
'''
_DIRECT_MERGE_STUB = r'''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["MERGE_MARKER"]).write_text("invoked\n")
print("merge stub ok")
'''


def _direct_tick_fleet(tmp_path: Path, mode: str):
    home, root = tmp_path / "home", tmp_path / "eda"
    gate = root / "fork-gatekeeper"
    gate.mkdir(parents=True)
    shutil.copy2(HERE / "run_tick.sh", gate / "run_tick.sh")
    shutil.copy2(
        HERE / "check_ai_release_authorization.py",
        gate / "check_ai_release_authorization.py")
    (root / "RELEASED.json").write_text('{"version":"0.0.1-test"}\n')
    registry = _registry(gate / "FORKS.json")

    for name in ("check_fork_only.py", "check_pins_agree.py",
                 "check_dockerfile_layer_budget.py", "check_doc_counts.py",
                 "check_fork_presence_claims.py"):
        _stub(root / "tools" / name, _DIRECT_OK_STUB)
    for name in ("check_our_commits_ship.py", "check_no_capability_lost.py",
                 "gatekeeper.py", "check_fork_selftests.py"):
        _stub(gate / name, _DIRECT_OK_STUB)
    _stub(gate / "daily_merge.py", _DIRECT_MERGE_STUB)
    _stub(gate / "daily_release.py", _DIRECT_RELEASE_STUB)
    _stub(home / ".local" / "bin" / "gh", r'''#!/usr/bin/env bash
[ "$1" = auth ] && echo gho_stubtoken
exit 0
''')

    round_record, boundary = _record(mode)
    state = tmp_path / "state"
    state.mkdir()
    round_json = state / "daily_0530.json"
    round_json.write_text(json.dumps(round_record))
    run_id = round_record["_round"]["run_id"]
    capability = "e" * 64
    args = [
        "--ai-round-json", str(round_json),
        "--ai-forks-json", str(registry),
        "--ai-round-at", boundary,
        "--ai-run-id", run_id,
        "--ai-capability", capability,
    ]
    env = dict(os.environ)
    env.update(
        HOME=str(home), GK_STATE_DIR=str(state),
        RELEASE_MARKER=str(tmp_path / "release-invoked"),
        MERGE_MARKER=str(tmp_path / "merge-invoked"),
    )
    return gate / "run_tick.sh", env, state, args, capability


def _direct_tick_handoff(state: Path, args: list[str], capability: str,
                         *, parent_pid: int | None = None) -> Path:
    values = dict(zip(args[::2], args[1::2]))
    handoff = state / f"ai-release-handoff.{capability}.json"
    handoff.write_text(json.dumps({
        "schema_version": 1,
        "capability": capability,
        "parent_pid": os.getpid() if parent_pid is None else parent_pid,
        "round_json": values["--ai-round-json"],
        "forks_json": values["--ai-forks-json"],
        "round_at": values["--ai-round-at"],
        "run_id": values["--ai-run-id"],
    }))
    return handoff


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("unverified", "blocked"), ("malformed", "norecord")],
)
def test_direct_real_tick_cannot_release_blocked_or_norecord_ai_round(
        tmp_path, mode, expected_status):
    script, env, state, args, capability = _direct_tick_fleet(tmp_path, mode)
    _direct_tick_handoff(state, args, capability)

    result = subprocess.run(
        ["bash", str(script), *args], env=env,
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 9
    assert not (tmp_path / "merge-invoked").exists()
    assert not (tmp_path / "release-invoked").exists()
    authorization = json.loads(
        (state / "ai-release-authorization.json").read_text())
    assert authorization["status"] == expected_status


def test_direct_real_tick_rechecks_authorized_context_before_release(tmp_path):
    script, env, state, args, capability = _direct_tick_fleet(
        tmp_path, "verified")
    _direct_tick_handoff(state, args, capability)

    result = subprocess.run(
        ["bash", str(script), *args], env=env,
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stdout[-2500:] + result.stderr[-1000:]
    assert (tmp_path / "merge-invoked").read_text() == "invoked\n"
    assert (tmp_path / "release-invoked").read_text() == "invoked\n"
    authorization = json.loads(
        (state / "ai-release-authorization.json").read_text())
    assert authorization["status"] == "authorized"
    assert authorization["round"]["run_id"] == "a" * 64


def test_direct_real_tick_without_wrapper_context_fails_before_merge(tmp_path):
    script, env, state, _args, _capability = _direct_tick_fleet(
        tmp_path, "verified")
    prior_release = state / "daily-release.json"
    prior_release.write_text(
        '{"program":"daily_release","status":"published"}\n')

    result = subprocess.run(
        ["bash", str(script)], env=env,
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 2
    assert "invoke run_0530.sh" in result.stdout
    assert not (tmp_path / "merge-invoked").exists()
    assert not (tmp_path / "release-invoked").exists()
    assert json.loads(prior_release.read_text())["status"] == "published"


def test_stale_parent_or_replayed_capability_cannot_release(tmp_path):
    script, env, state, args, capability = _direct_tick_fleet(
        tmp_path, "verified")
    prior_release = state / "daily-release.json"
    prior_release.write_text(
        '{"program":"daily_release","status":"published"}\n')
    _direct_tick_handoff(
        state, args, capability, parent_pid=os.getpid() + 100000)

    stale = subprocess.run(
        ["bash", str(script), *args], env=env,
        capture_output=True, text=True, timeout=30)
    assert stale.returncode == 9
    assert not (tmp_path / "merge-invoked").exists()
    assert not (tmp_path / "release-invoked").exists()
    assert json.loads(prior_release.read_text())["status"] == "published"

    # The stale claimant consumed the token. Recreating a matching context proves
    # one valid invocation works, while the exact same argv cannot be replayed.
    _direct_tick_handoff(state, args, capability)
    first = subprocess.run(
        ["bash", str(script), *args], env=env,
        capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stdout[-2500:] + first.stderr[-1000:]
    (tmp_path / "merge-invoked").unlink()
    (tmp_path / "release-invoked").unlink()

    replay = subprocess.run(
        ["bash", str(script), *args], env=env,
        capture_output=True, text=True, timeout=30)
    assert replay.returncode == 9
    assert not (tmp_path / "merge-invoked").exists()
    assert not (tmp_path / "release-invoked").exists()


def test_outer_round_maps_inner_authorization_refusal_to_norecord(tmp_path):
    script, env, state = _shell_fleet(tmp_path, "verified")
    _stub(script.parent / "run_tick.sh", "#!/usr/bin/env bash\nexit 9\n")
    prior_release = state / "daily-release.json"
    prior_release.parent.mkdir(parents=True, exist_ok=True)
    prior_release.write_text(
        '{"program":"daily_release","status":"published"}\n')

    result = subprocess.run(
        ["bash", str(script)], env=env,
        capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    captured = json.loads((state / "ll-captured.json").read_text())
    assert captured["tick_state"] == "not_checked_ai_norecord"
    assert "forced NORECORD" in (state / "daily_0530.log").read_text()
    assert json.loads(prior_release.read_text())["status"] == "published"


# ---------------------------------------------------------------------------
# A conflict that was resolved, landed, verified, and whose deferred ours-side
# discovery then RAN. Before 2026-08-20 the gate judged this on the label step 1
# wrote before the AI ran, so a fully-merged OpenROAD round returned NORECORD and
# froze https://vibeic.ai/eda-forks.html for three days. The outcome authorizes
# only WITH its evidence — the negative controls below are the load-bearing half.
# ---------------------------------------------------------------------------

def _landed(**over):
    landing = {"verified": True, "ours_resumed": True, "published_tip": "d" * 40}
    landing.update(over)
    return landing


def test_resolved_and_landed_upstream_conflict_authorizes_the_release():
    record, boundary = _record("verified")
    record["Demo"].update(upstream_merge_outcome="conflict_resolved",
                          upstream_landing=_landed())

    rc, result = _assess(record, boundary)

    assert rc == A.RC_AUTHORIZED, result.get("source_errors")
    assert not result["source_errors"]


@pytest.mark.parametrize("landing", [
    None,                                   # the outcome with no record at all
    {},                                     # a record proving nothing
    _landed(verified=False),                # landing not verified
    _landed(ours_resumed=False),            # ours side never resumed
    _landed(published_tip=""),              # no tip to point at
    _landed(published_tip="   "),           # whitespace is not a tip
    "d" * 40,                               # a string where a record belongs
])
def test_conflict_resolved_without_its_evidence_is_refused(landing):
    """The outcome is not a password. Claiming it without the record is a
    forgery and must be refused exactly as loudly as an unresolved conflict."""
    record, boundary = _record("verified")
    record["Demo"]["upstream_merge_outcome"] = "conflict_resolved"
    if landing is not None:
        record["Demo"]["upstream_landing"] = landing

    rc, result = _assess(record, boundary)

    assert rc == A.RC_NORECORD
    assert any("conflict_resolved" in error for error in result["source_errors"])


def test_conflict_resolved_cannot_paper_over_an_unmeasured_ours_side():
    """`ours_skipped` outranks the outcome: if branch discovery did not happen,
    no upstream evidence supplies it."""
    record, boundary = _record("verified")
    record["Demo"].update(
        upstream_merge_outcome="conflict_resolved",
        upstream_landing=_landed(),
        ours_skipped="BRANCH LIST UNKNOWN — origin fetch failed",
        merged=[],
    )

    rc, result = _assess(record, boundary)

    assert rc == A.RC_NORECORD
    assert any("ours_skipped" in error for error in result["source_errors"])
