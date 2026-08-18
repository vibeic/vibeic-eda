#!/usr/bin/env python3
"""Adversarial proofs for the opt-in watch-only mirror reset."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


W = _load("watch_only_reset")
U = _load("check_unshippable_patches")
A = _load("check_ai_release_authorization")
LL = _load("capture_release_ll")
AI_GATE_TEST = _load("test_ai_release_authorization_gate")
D0530 = _load("daily_0530")

RUN_ID = "0123456789abcdef0123456789abcdef"


def _run(*args: str, cwd: Path | None = None) -> str:
    cp = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr or cp.stdout
    return cp.stdout.strip()


def _tip(bare: Path, ref: str = "refs/heads/main") -> str:
    return _run("git", f"--git-dir={bare}", "rev-parse", ref)


def _policy() -> dict:
    return {
        "tool": "demo", "branch": "main", "upstream": "owner/demo",
        "mode": W.MODE, "preserve": W.PRESERVE,
        "reason": "A watch-only repository has no shipping consumer today.",
    }


def _registry(path: Path) -> Path:
    path.write_text(json.dumps({"forks": [{
        "tool": "demo", "branch": "main", "upstream": "owner/demo",
        "not_built_from": (
            "This repository is watched for upstream changes only and has no "
            "current product or image consumer."),
        "watch_only_reset": {"mode": W.MODE, "preserve": W.PRESERVE},
    }]}) + "\n")
    return path


def _fleet(tmp_path: Path, *, divergent: bool = True) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    upstream = tmp_path / "upstream.git"
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    fork_seed = tmp_path / "fork-seed"
    repo = tmp_path / "repo"
    _run("git", "init", "--bare", str(upstream))
    _run("git", "init", "-b", "main", str(seed))
    _run("git", "config", "user.name", "Test", cwd=seed)
    _run("git", "config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "payload").write_text("upstream\n")
    _run("git", "add", "payload", cwd=seed)
    _run("git", "commit", "-m", "upstream", cwd=seed)
    _run("git", "remote", "add", "origin", str(upstream), cwd=seed)
    _run("git", "push", "origin", "main", cwd=seed)
    _run("git", f"--git-dir={upstream}", "symbolic-ref", "HEAD",
         "refs/heads/main")
    target = _tip(upstream)

    _run("git", "clone", str(upstream), str(fork_seed))
    _run("git", "config", "user.name", "Test", cwd=fork_seed)
    _run("git", "config", "user.email", "test@example.invalid", cwd=fork_seed)
    if divergent:
        (fork_seed / "fork-only").write_text("not shipped\n")
        _run("git", "add", "fork-only", cwd=fork_seed)
        _run("git", "commit", "-m", "fork only", cwd=fork_seed)
    old = _run("git", "rev-parse", "HEAD", cwd=fork_seed)
    _run("git", "init", "--bare", str(origin))
    _run("git", "remote", "add", "fork-origin", str(origin), cwd=fork_seed)
    _run("git", "push", "fork-origin", "main", cwd=fork_seed)
    _run("git", f"--git-dir={origin}", "symbolic-ref", "HEAD",
         "refs/heads/main")

    _run("git", "clone", str(origin), str(repo))
    _run("git", "config", "user.name", "Test", cwd=repo)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _run("git", "remote", "add", "upstream", str(upstream), cwd=repo)
    return {"repo": repo, "origin": origin, "upstream": upstream,
            "fork_seed": fork_seed, "old": old, "target": target}


def _apply(fleet: dict, *, apply: bool = True) -> dict:
    def guard():
        return True, {"outcome": "complete", "open_count": 0, "items": [],
                      "upstream_repository": "owner/demo",
                      "fork_owner": "vibeic"}, ""

    return W.apply_policy(
        fleet["repo"], _policy(), apply=apply,
        round_at=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
        allow_non_github_remotes=True, destructive_guard=guard)


def _round(result: dict, registry: Path) -> tuple[dict, str]:
    round_at = result["round_at"]
    policies = W.load_registry(registry)
    value = {
        "_round": {"schema_version": 1, "run_id": RUN_ID,
                   "started_at": round_at,
                   "finished_at": datetime.now(timezone.utc).isoformat(),
                   "applied": True},
        "_watch_only_resets": W.make_round_evidence(
            round_at=round_at, run_id=RUN_ID, applied=True,
            registry=policies, results=[result]),
    }
    return value, round_at


def test_reset_preserves_exact_old_tip_then_uses_branch_lease(tmp_path):
    fleet = _fleet(tmp_path)
    result = _apply(fleet)
    assert result["status"] == "pass" and result["outcome"] == "reset", result
    assert _tip(fleet["origin"]) == fleet["target"]
    assert _tip(fleet["origin"], result["recovery_ref"]) == fleet["old"]
    assert _run("git", "rev-parse", "main", cwd=fleet["repo"]) == fleet["target"]


def test_dry_run_changes_no_ref_worktree_or_remote(tmp_path):
    fleet = _fleet(tmp_path)
    objects = fleet["repo"] / ".git" / "objects"
    before = (_run("git", "show-ref", cwd=fleet["repo"]),
              _run("git", "status", "--porcelain=v1", cwd=fleet["repo"]),
              _run("git", "show-ref", cwd=fleet["origin"]),
              sorted(str(path.relative_to(objects)) for path in objects.rglob("*")
                     if path.is_file()))
    result = _apply(fleet, apply=False)
    after = (_run("git", "show-ref", cwd=fleet["repo"]),
             _run("git", "status", "--porcelain=v1", cwd=fleet["repo"]),
             _run("git", "show-ref", cwd=fleet["origin"]),
             sorted(str(path.relative_to(objects)) for path in objects.rglob("*")
                    if path.is_file()))
    assert result["outcome"] == "would_reset"
    assert after == before


def test_no_policy_refuses_without_mutation(tmp_path):
    fleet = _fleet(tmp_path)
    result = W.apply_policy(
        fleet["repo"], None, apply=True,
        round_at=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
        allow_non_github_remotes=True)
    assert result["status"] == "blocked"
    assert _tip(fleet["origin"]) == fleet["old"]


def test_unknown_upstream_ref_is_norecord_and_does_not_mutate(tmp_path):
    fleet = _fleet(tmp_path)
    _run("git", f"--git-dir={fleet['upstream']}", "update-ref", "-d",
         "refs/heads/main")
    result = _apply(fleet)
    assert result["status"] == "norecord"
    assert _tip(fleet["origin"]) == fleet["old"]


def test_clean_already_aligned_is_exact_noop(tmp_path):
    fleet = _fleet(tmp_path, divergent=False)
    result = _apply(fleet)
    assert result["status"] == "pass"
    assert result["outcome"] == "already_aligned"
    assert result["old_tip"] == result["remote_tip_after"] == fleet["target"]


def test_recovery_tag_collision_blocks_before_branch_mutation(tmp_path):
    fleet = _fleet(tmp_path)
    ref = f"{W.RECOVERY_PREFIX}/{fleet['old']}"
    _run("git", f"--git-dir={fleet['origin']}", "update-ref", ref,
         fleet["target"])
    result = _apply(fleet)
    assert result["status"] == "blocked"
    assert "collision" in result["reason"]
    assert _tip(fleet["origin"]) == fleet["old"]


def test_concurrent_remote_change_wins_and_branch_cas_fails(monkeypatch, tmp_path):
    fleet = _fleet(tmp_path)
    seed = fleet["fork_seed"]
    (seed / "racer").write_text("race\n")
    _run("git", "add", "racer", cwd=seed)
    _run("git", "commit", "-m", "racer", cwd=seed)
    rival = _run("git", "rev-parse", "HEAD", cwd=seed)
    _run("git", "push", "fork-origin", "HEAD:refs/heads/race", cwd=seed)
    original = W._git
    raced = False

    def race(repo, *args, **kwargs):
        nonlocal raced
        if (not raced and "push" in args
                and any(str(a).startswith(
                    "--force-with-lease=refs/heads/main:") for a in args)):
            raced = True
            _run("git", f"--git-dir={fleet['origin']}", "update-ref",
                 "refs/heads/main", rival)
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(W, "_git", race)
    result = _apply(fleet)
    assert raced and result["status"] == "blocked"
    assert "CAS push failed" in result["reason"]
    assert _tip(fleet["origin"]) == rival
    assert _tip(fleet["origin"], result["recovery_ref"]) == fleet["old"]


def test_partial_remote_success_repairs_only_a_recovery_tagged_local_tip(tmp_path):
    fleet = _fleet(tmp_path)
    first = _apply(fleet)
    assert first["outcome"] == "reset"
    _run("git", "update-ref", "refs/heads/main", fleet["old"],
         fleet["target"], cwd=fleet["repo"])
    _run("git", "reset", "--hard", fleet["old"], cwd=fleet["repo"])
    repaired = _apply(fleet)
    assert repaired["status"] == "pass"
    assert repaired["outcome"] == "repaired_local"
    assert repaired["local_old_tip"] == fleet["old"]


def test_aligned_remote_never_discards_an_untagged_local_commit(tmp_path):
    fleet = _fleet(tmp_path)
    assert _apply(fleet)["outcome"] == "reset"
    (fleet["repo"] / "local-only").write_text("must survive\n")
    _run("git", "add", "local-only", cwd=fleet["repo"])
    _run("git", "commit", "-m", "arbitrary local work", cwd=fleet["repo"])
    arbitrary = _run("git", "rev-parse", "HEAD", cwd=fleet["repo"])
    refused = _apply(fleet)
    assert refused["status"] == "blocked"
    assert "without its exact deterministic recovery tag" in refused["reason"]
    assert _run("git", "rev-parse", "HEAD", cwd=fleet["repo"]) == arbitrary
    assert (fleet["repo"] / "local-only").read_text() == "must survive\n"


def test_untracked_or_ignored_obstruction_is_never_discarded(tmp_path):
    fleet = _fleet(tmp_path)
    (fleet["repo"] / ".git" / "info" / "exclude").write_text("obstruction\n")
    (fleet["repo"] / "obstruction").write_text("keep me\n")
    result = _apply(fleet)
    assert result["status"] == "blocked"
    assert (fleet["repo"] / "obstruction").read_text() == "keep me\n"
    assert _tip(fleet["origin"]) == fleet["old"]


def test_exact_github_identity_does_not_accept_host_suffixes():
    assert W._github_slug("https://github.com/vibeic/demo.git") == "vibeic/demo"
    assert W._github_slug("git@github.com:vibeic/demo.git") == "vibeic/demo"
    assert W._github_slug("https://evilgithub.com/vibeic/demo") is None
    assert W._github_slug("https://github.com.evil/vibeic/demo") is None


def test_pr_opened_at_branch_cas_boundary_blocks_rewrite(tmp_path):
    fleet = _fleet(tmp_path)

    def opened():
        return False, {
            "outcome": "complete", "open_count": 1,
            "items": [{"number": 7}], "upstream_repository": "owner/demo",
            "fork_owner": "vibeic",
        }, "one PR opened"

    result = W.apply_policy(
        fleet["repo"], _policy(), apply=True,
        round_at=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
        allow_non_github_remotes=True, destructive_guard=opened)
    assert result["status"] == "blocked"
    assert result["branch_cas_guard"]["open_count"] == 1
    assert _tip(fleet["origin"]) == fleet["old"]


def test_wrong_pr_actor_is_norecord_at_branch_cas_boundary(tmp_path):
    fleet = _fleet(tmp_path)

    def wrong_actor():
        return True, {
            "outcome": "complete", "open_count": 0, "items": [],
            "upstream_repository": "owner/demo", "fork_owner": "attacker",
        }, ""

    result = W.apply_policy(
        fleet["repo"], _policy(), apply=True,
        round_at=datetime.now(timezone.utc).isoformat(), run_id=RUN_ID,
        allow_non_github_remotes=True, destructive_guard=wrong_actor)
    assert result["status"] == "norecord"
    assert _tip(fleet["origin"]) == fleet["old"]


def test_registry_and_evidence_json_reject_duplicates_and_nonfinite(tmp_path):
    registry = tmp_path / "FORKS.json"
    registry.write_text('{"forks":[],"forks":[]}')
    assert W.load_registry(registry)["errors"]
    evidence = tmp_path / "daily.json"
    evidence.write_text('{"x":NaN}')
    with pytest.raises(ValueError):
        W.read_json(evidence)


def test_release_authorizer_strictly_rejects_duplicate_round_json(tmp_path):
    registry = _registry(tmp_path / "FORKS.json")
    source = tmp_path / "daily.json"
    source.write_text('{"_round":{},"_round":{}}')
    output = tmp_path / "authorization.json"
    rc = A.main([
        "--round-json", str(source), "--forks-json", str(registry),
        "--round-at", datetime.now(timezone.utc).isoformat(),
        "--json", str(output),
    ])
    assert rc == A.RC_NORECORD
    record = json.loads(output.read_text())
    assert any("duplicate JSON key" in error
               for error in record["source_errors"])


def test_ll_strictly_rejects_and_retains_duplicate_reset_json(tmp_path):
    registry = _registry(tmp_path / "FORKS.json")
    duplicate = tmp_path / "daily.json"
    duplicate.write_text('{"_round":{},"_round":{}}')
    missing = tmp_path / "missing.json"
    round_at = datetime.now(timezone.utc).isoformat()
    rc = LL.main([
        "--state-dir", str(tmp_path / "state"),
        "--eda-root", str(HERE.parent),
        "--release-json", str(missing),
        "--release-pins-json", str(missing),
        "--gap-json", str(missing),
        "--fork-github-json", str(missing),
        "--ai-authorization-json", str(missing),
        "--watch-reset-json", str(duplicate),
        "--expected-github-actor", "reyerchu",
        "--forks-json", str(registry),
        "--round-at", round_at, "--tick-exit", "2",
        "--tick-state", "not_checked_pytest_red",
    ])
    assert rc == 1
    latest = json.loads(
        (tmp_path / "state" / "release-ll" / "latest.json").read_text())
    assert any("strict watch-reset source read failed" in error
               and "duplicate JSON key" in error
               for error in latest["source_errors"])


def test_ll_main_cannot_false_green_when_policy_input_is_omitted(tmp_path):
    registry = _registry(tmp_path / "FORKS.json")
    missing = tmp_path / "missing.json"
    rc = LL.main([
        "--state-dir", str(tmp_path / "state"),
        "--eda-root", str(HERE.parent),
        "--release-json", str(missing),
        "--release-pins-json", str(missing),
        "--gap-json", str(missing),
        "--fork-github-json", str(missing),
        "--ai-authorization-json", str(missing),
        "--expected-github-actor", "reyerchu",
        "--forks-json", str(registry),
        "--round-at", datetime.now(timezone.utc).isoformat(),
        "--tick-exit", "2", "--tick-state", "not_checked_pytest_red",
    ])
    assert rc == 1
    latest = json.loads(
        (tmp_path / "state" / "release-ll" / "latest.json").read_text())
    assert any("--watch-reset-json is required" in error
               for error in latest["source_errors"])
    assert latest["convergence"]["converged"] is False


def test_current_exact_reset_supersedes_only_its_stale_semantic_row(tmp_path):
    fleet = _fleet(tmp_path)
    result = _apply(fleet)
    registry = _registry(tmp_path / "FORKS.json")
    evidence, round_at = _round(result, registry)
    evidence_path = tmp_path / "daily.json"
    evidence_path.write_text(json.dumps(evidence))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "demo.json").write_text(json.dumps(
        {"tool": "demo", "ahead": 3, "integrated": False}))

    rc, message = U.report(
        ledger, watch_reset_json=evidence_path, round_at=round_at,
        forks_json=registry)
    assert rc == 0 and "supersedes stale ledger" in message

    forged = copy.deepcopy(evidence)
    forged["_watch_only_resets"]["results"][0]["remote_tip_after"] = "f" * 40
    evidence_path.write_text(json.dumps(forged))
    rc, message = U.report(
        ledger, watch_reset_json=evidence_path, round_at=round_at,
        forks_json=registry)
    assert rc == 1 and "evidence rejected" in message

    wrong_owner = copy.deepcopy(evidence)
    wrong_owner["_watch_only_resets"]["results"][0][
        "branch_cas_guard"]["fork_owner"] = "someone-else"
    _summary, errors = W.validate_round_evidence(
        wrong_owner, round_at=round_at, forks_json=registry)
    assert any("PR recheck" in error for error in errors)


def test_round_validator_rejects_timestamp_and_divergence_forgery(tmp_path):
    fleet = _fleet(tmp_path)
    result = _apply(fleet)
    registry = _registry(tmp_path / "FORKS.json")
    evidence, round_at = _round(result, registry)
    mutations = [
        (lambda row: row.update(started_at="2000-01-01T00:00:00+00:00"),
         "timestamps"),
        (lambda row: row.update(finished_at="2099-01-01T00:00:00+00:00"),
         "timestamps"),
        (lambda row: row.update(ahead=True), "divergence counts"),
        (lambda row: row.update(run_id="f" * 32), "envelope"),
    ]
    for mutate, needle in mutations:
        forged = copy.deepcopy(evidence)
        mutate(forged["_watch_only_resets"]["results"][0])
        _summary, errors = W.validate_round_evidence(
            forged, round_at=round_at, forks_json=registry)
        assert any(needle in error for error in errors), errors


def test_daily_round_boundary_env_and_cli_bind_the_same_nonce(
        tmp_path, monkeypatch):
    forks = tmp_path / "forks"
    forks.mkdir()
    registry = {"policies": {}, "invalid_tools": {}, "errors": [],
                "digest": "0" * 64}
    monkeypatch.setattr(D0530, "FORKS", forks)
    monkeypatch.setattr(D0530, "image_pins", lambda *a, **k: {})
    monkeypatch.setattr(D0530.watch_only_reset, "load_registry",
                        lambda _path: registry)
    monkeypatch.setattr(
        D0530, "step2b_ai_decisions",
        lambda *a, **k: {"cases": 0, "invoked": False,
                         "verified": True, "cleared": True})
    monkeypatch.setattr(
        D0530.round_record, "write",
        lambda *a, **k: {"stamp": "test", "rows": 0, "pruned": []})
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path / "state"))
    env_boundary = "2026-08-18T05:30:00+08:00"
    cli_boundary = "2026-08-18T05:31:00+08:00"

    monkeypatch.setenv("GK_CURRENT_ROUND_AT", env_boundary)
    env_json = tmp_path / "env.json"
    assert D0530.main_(["--json", str(env_json), "--skip-build"]) == 0
    env_record = json.loads(env_json.read_text())
    assert env_record["_watch_only_resets"]["round_at"] == env_boundary
    assert env_record["_watch_only_resets"]["run_id"] == env_record[
        "_round"]["run_id"]

    cli_json = tmp_path / "cli.json"
    assert D0530.main_([
        "--round-at", cli_boundary, "--json", str(cli_json), "--skip-build",
    ]) == 0
    cli_record = json.loads(cli_json.read_text())
    assert cli_record["_watch_only_resets"]["round_at"] == cli_boundary
    assert cli_record["_watch_only_resets"]["run_id"] == cli_record[
        "_round"]["run_id"]


@pytest.mark.parametrize("divergent", [True, False])
def test_ll_independently_validates_and_retains_exact_reset_or_noop(
        tmp_path, divergent):
    fleet = _fleet(tmp_path / "fleet", divergent=divergent)
    result = _apply(fleet)
    registry = _registry(tmp_path / "FORKS.json")
    evidence, round_at = _round(result, registry)
    summary, errors = W.validate_round_evidence(
        evidence, round_at=round_at, forks_json=registry)
    assert errors == []
    assert summary["results"][0]["outcome"] == (
        "reset" if divergent else "already_aligned")
    record = LL.make_record(
        {"status": "published", "compose_attempts": [{"exit_code": 0}]},
        {"q1_sync_lag_at_merge": 0, "q1_sync_lag": 0,
         "q1_image_behind_upstream": 0, "q1_release_lag": 0,
         "q2_ours_past_the_pin": 0, "q2_our_commits_stranded": 0},
        round_at, tick_exit=0, watch_reset=summary)
    written = LL.retain(record, tmp_path / "state")
    full = json.loads(Path(written["full"]).read_text())
    narrow = json.loads(
        (tmp_path / "state" / "release-ll" / "index.jsonl")
        .read_text().splitlines()[-1])
    assert full["watch_only_reset"]["results"] == summary["results"]
    assert narrow["watch_only_reset"]["results"] == summary["results"]
    assert full["convergence"]["watch_only_reset_clear"] is True


@pytest.mark.parametrize("status", ["blocked", "norecord"])
def test_ll_retains_distinct_valid_negative_reset_outcome(tmp_path, status):
    registry = _registry(tmp_path / "FORKS.json")
    policies = W.load_registry(registry)
    round_at = datetime.now(timezone.utc).isoformat()
    result = W.refusal_record(
        policies["policies"]["demo"], apply=True, round_at=round_at,
        run_id=RUN_ID, status=status, reason="measured prerequisite refused")
    value = {
        "_round": {"schema_version": 1, "run_id": RUN_ID,
                   "started_at": round_at,
                   "finished_at": datetime.now(timezone.utc).isoformat(),
                   "applied": True},
        "_watch_only_resets": W.make_round_evidence(
            round_at=round_at, run_id=RUN_ID, applied=True,
            registry=policies, results=[result]),
    }
    summary, errors = W.validate_round_evidence(
        value, round_at=round_at, forks_json=registry)
    assert errors == [] and summary["status"] == status
    record = LL.make_record(
        {"status": "not_checked"}, {}, round_at, tick_exit=2,
        source_errors=["release authorization withheld"],
        tick_state="not_checked_ai_norecord", watch_reset=summary)
    assert record["lesson"]["outcome"] == f"watch_only_reset_{status}"
    assert record["lesson"]["trigger"] == f"watch_only_reset_{status}"
    written = LL.retain(record, tmp_path / "state")
    retained = json.loads(Path(written["full"]).read_text())
    assert retained["watch_only_reset"]["results"][0]["reason"] == (
        "measured prerequisite refused")


@pytest.mark.parametrize(
    ("watch_status", "expected_rc"),
    [("pass", 0), ("blocked", 1), ("norecord", 2)],
)
def test_pre_release_authorization_requires_current_watch_pass(
        watch_status, expected_rc):
    now = datetime.now(timezone.utc).isoformat()
    round_record = {
        "demo": {"main": "main", "conflicted": [],
                 "upstream_merge_outcome": "natural_success"},
        "_ai_decisions": {"cases": 0, "invoked": False, "verified": True,
                          "verified_cases": 0, "verification": [],
                          "unverified_cases": []},
        "_round": {"schema_version": 1, "run_id": RUN_ID,
                   "started_at": now, "finished_at": now, "applied": True},
    }
    rc, _record = A.assess(
        round_record, now, registered_forks=["demo"],
        watch_reset={"status": watch_status})
    assert rc == expected_rc


@pytest.mark.parametrize(
    ("watch_status", "tick_runs", "auth_status"),
    [("pass", True, "authorized"),
     ("blocked", False, "blocked"),
     ("missing", False, "norecord"),
     ("malformed", False, "norecord")],
)
def test_real_0530_wrapper_never_releases_without_current_reset_pass(
        tmp_path, watch_status, tick_runs, auth_status):
    script, env, state = AI_GATE_TEST._shell_fleet(tmp_path, "no_cases")
    gate = script.parent
    shutil.copy2(HERE / "watch_only_reset.py", gate / "watch_only_reset.py")
    shutil.copy2(HERE / "progress_watchdog.py", gate / "progress_watchdog.py")
    registry_path = gate / "FORKS.json"
    registry = json.loads(registry_path.read_text())
    row = registry["forks"][0]
    row["branch"] = "main"
    row["upstream"] = "owner/Demo"
    row["not_built_from"] = (
        "This repository is watched for upstream changes only and has no "
        "current product or image consumer.")
    row["watch_only_reset"] = {"mode": W.MODE, "preserve": W.PRESERVE}
    registry_path.write_text(json.dumps(registry))
    AI_GATE_TEST._stub(gate / "daily_0530.py", r'''#!/usr/bin/env python3
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import watch_only_reset as W
p=argparse.ArgumentParser(); p.add_argument("--apply",action="store_true")
p.add_argument("--ai-stall-grace"); p.add_argument("--json"); a=p.parse_args()
round_at=os.environ["GK_CURRENT_ROUND_AT"]
run_id="a"*64; started=datetime.now(timezone.utc)
record={"Demo":{"main":"main","conflicted":[],
 "upstream_merge_outcome":"natural_success"},
 "_ai_decisions":{"cases":0,"invoked":False,"cleared":True,"verified":True}}
mode=os.environ["WATCH_FIXTURE"]
registry=W.load_registry(Path(__file__).with_name("FORKS.json"))
policy=registry["policies"]["Demo"]
if mode == "pass":
 tip="b"*40
 result={"program":"watch_only_reset","schema_version":1,"tool":"Demo",
  "round_at":round_at,"run_id":run_id,"started_at":started.isoformat(),
  "finished_at":datetime.now(timezone.utc).isoformat(),"applied":True,
  "status":"pass","outcome":"already_aligned","reason":None,
  "branch":"main","upstream_repository":"owner/Demo",
  "policy":{"mode":W.MODE,"preserve":W.PRESERVE},"old_tip":tip,
  "upstream_tip":tip,"local_old_tip":None,"ahead":0,"behind":0,
  "recovery_ref":None,"recovery_tip":None,"recovery_action":None,
  "remote_tip_after":tip,"upstream_tip_after":tip,"local_tip_after":tip,
  "branch_cas_guard":None,
  "cas":{"applied":False,"expected_old":tip,"target":tip,"verified":True}}
 record["_watch_only_resets"]=W.make_round_evidence(
  round_at=round_at,run_id=run_id,applied=True,registry=registry,results=[result])
elif mode == "blocked":
 result=W.refusal_record(policy,apply=True,round_at=round_at,run_id=run_id,
  status="blocked",reason="upstream PR opened")
 record["_watch_only_resets"]=W.make_round_evidence(
  round_at=round_at,run_id=run_id,applied=True,registry=registry,results=[result])
elif mode == "malformed": record["_watch_only_resets"]={}
finished=datetime.now(timezone.utc)
record["_round"]={"schema_version":1,"run_id":run_id,
 "started_at":started.isoformat(),"finished_at":finished.isoformat(),"applied":True}
Path(a.json).write_text(json.dumps(record))
sys.exit(1)
''')
    # A clean prior ledger cannot substitute for today's reset proof.
    ledger = state / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "Demo.json").write_text(json.dumps(
        {"tool": "Demo", "ahead": 0, "integrated": False}))
    env["WATCH_FIXTURE"] = watch_status
    completed = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True,
        timeout=30)
    assert completed.returncode == 1
    assert (state / "run-tick.called").exists() is tick_runs
    captured = json.loads((state / "ll-captured.json").read_text())
    assert captured["ai"]["status"] == auth_status
