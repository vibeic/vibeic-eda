"""The public fork page is a current-round certificate, not a best-effort view.

These tests run the real ``run_0530.sh`` with small process-boundary stubs.  The
important assertion is whether the page builder was invoked: a late gate that
failed after discovery must leave the previous page alone, just as an early
semantic or AI gate does.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import build_page as PAGE
import capture_release_ll as LL
import test_ai_handoff_progress_supervision as AI_FIX
import test_fork_github_convergence_ll as GH_FIX
from test_ai_release_authorization_gate import _shell_fleet, _stub


HERE = Path(__file__).resolve().parent
EDA_VERSION = subprocess.run(
    ["git", "-C", str(HERE.parent), "show", "HEAD:VERSION"],
    check=True, capture_output=True, text=True,
).stdout.strip()
RELEASE_RECORD_COMMIT = subprocess.run(
    ["git", "-C", str(HERE.parent), "log", "-1", "--format=%H", "HEAD",
     "--", "RELEASED.json"],
    check=True, capture_output=True, text=True,
).stdout.strip()
# An ancestor that is NOT the release-record commit, BY CONSTRUCTION.
# This used to be `HEAD^`, which collides with RELEASE_RECORD_COMMIT in a
# completely ordinary state: exactly one commit sitting on top of a release.
# That is what the tree looks like after any single post-release fix, so the
# test's own precondition (`ANCESTOR_COMMIT != RELEASE_RECORD_COMMIT`) failed
# and took the impersonation check with it -- the guard was not exercised at
# all, which is worse than a visible red. Walking back from the release record
# cannot collide with it.
ANCESTOR_COMMIT = subprocess.run(
    ["git", "-C", str(HERE.parent), "rev-parse", RELEASE_RECORD_COMMIT + "^"],
    check=True, capture_output=True, text=True,
).stdout.strip()
FIXTURE_NOW = datetime.fromisoformat("2026-08-17T06:10:00+08:00")


def _recording_page_builder(path: Path) -> None:
    _stub(path, r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ["GK_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
state.joinpath("build-page.called.json").write_text(json.dumps(sys.argv[1:]))
raise SystemExit(0)
''')


def _run(tmp_path: Path, *, late_gate: str | None = None,
         ai_mode: str = "verified"):
    script, env, state = _shell_fleet(tmp_path, ai_mode)
    _recording_page_builder(script.parent / "build_page.py")

    if late_gate == "tick":
        _stub(script.parent / "run_tick.sh", "#!/usr/bin/env bash\nexit 1\n")
    elif late_gate == "discover":
        _stub(script.parent / "discover_forks.py",
              "#!/usr/bin/env python3\nraise SystemExit(1)\n")
    elif late_gate == "gap":
        _stub(script.parent / "fork_gap_report.py", r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path(sys.argv[sys.argv.index("--json")+1]).write_text(json.dumps({
 "q1_sync_lag_at_merge":0,"q1_sync_lag":0,
 "q2_our_commits_stranded":0,"q2_ours_past_the_pin_substantive":0}))
raise SystemExit(1)
''')
    elif late_gate in {"github_open", "github_norecord"}:
        rc = 1 if late_gate == "github_open" else 2
        _stub(script.parent / "fork_github_convergence.py", f'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path(sys.argv[sys.argv.index("--json")+1]).write_text(json.dumps({{"status":"fixture"}}))
raise SystemExit({rc})
''')
    elif late_gate == "ll":
        _stub(script.parent / "capture_release_ll.py", r'''#!/usr/bin/env python3
import os
from pathlib import Path
Path(os.environ["GK_STATE_DIR"]).joinpath("ll-ran").write_text("yes")
raise SystemExit(1)
''')
    elif late_gate == "freshness":
        _stub(script.parent / "check_ledger_is_fresh.py",
              "#!/usr/bin/env python3\nraise SystemExit(1)\n")

    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True,
        timeout=30,
    )
    return result, state


def test_page_build_is_ordered_after_every_late_publication_source():
    root = Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=True,
        capture_output=True, text=True,
    ).stdout.strip())
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch",
         "fork-gatekeeper/run_0530.sh", "fork-gatekeeper/FORKS.json"],
        cwd=root, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, tracked.stderr
    source = (root / "fork-gatekeeper" / "run_0530.sh").read_text(
        encoding="utf-8")
    page = source.index('python3 "${DIR}/build_page.py"')
    for producer in (
        'python3 "${DIR}/fork_gap_report.py"',
        'python3 "${DIR}/fork_github_convergence.py"',
        'python3 "${DIR}/capture_release_ll.py"',
        'python3 "${DIR}/check_ledger_is_fresh.py"',
    ):
        assert source.index(producer) < page, (
            f"{producer} runs after the public page was already replaced")


@pytest.mark.parametrize("late_gate", [
    "tick", "discover", "gap", "github_open", "github_norecord", "ll",
    "freshness",
])
def test_each_nonzero_late_gate_preserves_the_last_good_page(
        tmp_path, late_gate):
    result, state = _run(tmp_path, late_gate=late_gate)

    assert result.returncode == 1
    assert not (state / "build-page.called.json").exists(), (
        f"build_page ran even though the current-round {late_gate} gate was nonzero")
    log = (state / "daily_0530.log").read_text(encoding="utf-8")
    assert "build_page SKIPPED" in log


def test_ai_skip_cannot_republish_a_stale_daily_release(tmp_path):
    prior_release = tmp_path / "state" / "daily-release.json"
    prior_release.parent.mkdir(parents=True)
    prior = {"program": "daily_release", "schema_version": 2,
             "status": "published", "started_at": "2026-08-16T05:40:00+08:00"}
    prior_release.write_text(json.dumps(prior), encoding="utf-8")

    result, state = _run(tmp_path, ai_mode="unverified")

    assert result.returncode == 1
    assert json.loads(prior_release.read_text(encoding="utf-8")) == prior
    assert not (state / "build-page.called.json").exists(), (
        "an AI-skipped round let build_page attribute yesterday's release to today")


def test_fully_converged_zero_round_reaches_builder_with_exact_evidence(tmp_path):
    result, state = _run(tmp_path)

    # The synthetic daily_0530 producer deliberately returns 1 after writing a
    # complete verified round.  That keeps the wrapper's broad outcome red, but
    # every publication authority is current and zero, so the narrower page
    # boundary must still run.
    assert result.returncode == 1
    args = json.loads(
        (state / "build-page.called.json").read_text(encoding="utf-8"))
    for option in (
        "--round-at", "--run-id", "--forks-json", "--gap-json",
        "--fork-github-json", "--release-ll-json",
        "--expected-github-actor",
    ):
        assert option in args


def _strict_fixture(tmp_path: Path, *, github: dict | None = None,
                    gap: dict | None = None) -> dict:
    gap = copy.deepcopy(gap or GH_FIX._gap_record())
    github = copy.deepcopy(github or GH_FIX._fork_record())
    gap.update(
        started_at="2026-08-17T06:06:00+08:00",
        completed_at="2026-08-17T06:07:00+08:00",
    )
    github.update(
        started_at="2026-08-17T06:08:00+08:00",
        completed_at="2026-08-17T06:09:00+08:00",
    )
    ai = GH_FIX._ai_authorization_record()
    pins = AI_FIX._release_pins_record(EDA_VERSION)
    pins["committed"] = True
    pins["requested_revision"] = "HEAD"
    pins["checked_at"] = "2026-08-17T06:04:30+08:00"
    manifest_identity = "e" * 64
    record_identity = RELEASE_RECORD_COMMIT
    release = {
        "program": "daily_release", "schema_version": 2,
        "started_at": "2026-08-17T06:03:30+08:00",
        "finished_at": "2026-08-17T06:04:00+08:00",
        "exit_code": 0,
        "status": "noop", "moved": [], "built": [],
        "compose_attempts": [], "failure": None,
        "version": {"from": EDA_VERSION, "to": EDA_VERSION},
        "smoke": {"executed": False, "expected": 16, "passed": 0,
                  "outcome": "not_applicable", "failures": []},
        "remote_publication": {
            "verified": True,
            "manifests": {
                "version": {
                    "ref": f"ghcr.io/vibeic/vibeic-eda:{EDA_VERSION}",
                    "state": "present", "source": "registry",
                    "identity": manifest_identity, "detail": "fixture",
                },
                "latest": {
                    "ref": "ghcr.io/vibeic/vibeic-eda:latest",
                    "state": "present", "source": "registry",
                    "identity": manifest_identity, "detail": "fixture",
                },
            },
        },
        "release_record_publication": {
            "verified": True, "subject": "origin/main:RELEASED.json",
            "identity": record_identity, "initial_state": "present",
            "repair_attempted": False,
        },
        "anchor_pr": {"ok": True,
                      "note": f"main already anchors {EDA_VERSION}"},
    }
    semantic = {
        "schema": 1,
        "mechanism": "validated_semantic_progress_lease",
        "started_at": "2026-08-17T06:02:30+08:00",
        "exit_code": 0, "outcome": "natural", "reason": "fixture",
        "process_rc": 0,
        "elapsed_seconds": 1.0, "stall_grace_seconds": 300,
        "total_runtime_ceiling_seconds": None,
        "junit": "/state/fork-gatekeeper.junit.xml",
        "junit_cases": 1, "junit_red": 0,
        "progress": {"events": 3, "last_event": "session_finish",
                     "stage": "finished", "selected": 1, "finished": 1,
                     "exitstatus": 0, "protocol_error": None},
        "process_tree": {"subreaper": True, "census_complete": True,
                         "live_descendants_observed": [],
                         "cleanup_observed": [], "cleanup_survivors": [],
                         "final_empty": True},
    }
    watch_source = json.loads(
        GH_FIX._watch_reset_record(tmp_path / "watch-round.json").read_text())
    watch, watch_errors = LL.watch_only_reset.validate_round_evidence(
        watch_source, round_at=GH_FIX.ROUND_AT,
        forks_json=HERE / "FORKS.json")
    assert watch_errors == [] and watch is not None
    ll = LL.make_record(
        release, gap, GH_FIX.ROUND_AT, 0, [],
        semantic_pytest=semantic, tick_state="ran", fork_github=github,
        ai_authorization=ai, release_pins=pins, watch_reset=watch,
    )
    # ``make_record`` normally stamps the real capture clock.  Pin this fixture
    # so freshness tests prove exactly the intended boundary on every test day.
    ll["captured_at"] = FIXTURE_NOW.isoformat()

    paths = {
        "forks": HERE / "FORKS.json",
        "gap": tmp_path / "fork-gap.json",
        "github": tmp_path / "fork-github.json",
        "ll": tmp_path / "release-ll.json",
    }
    paths["gap"].write_text(json.dumps(gap), encoding="utf-8")
    paths["github"].write_text(json.dumps(github), encoding="utf-8")
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")
    paths.update(gap_record=gap, github_record=github, ll_record=ll)
    return paths


def _load_strict(paths: dict, *, now: datetime = FIXTURE_NOW):
    return PAGE.load_publication_evidence(
        round_at=GH_FIX.ROUND_AT, run_id="a" * 64,
        forks_json=paths["forks"], gap_json=paths["gap"],
        fork_github_json=paths["github"], release_ll_json=paths["ll"],
        expected_github_actor="reyerchu",
        now=now,
    )


def _unmeasured_github() -> dict:
    record = GH_FIX._fork_record()
    record["status"] = "norecord"
    record["source_errors"] = [
        {"source": "fixture", "error": "unmeasured"}]
    return record


def _page_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "state"
    ledger = state / "ledger"
    ledger.mkdir(parents=True)
    for tool in GH_FIX.REGISTERED_FORKS:
        (ledger / f"{tool}.json").write_text(json.dumps({
            "tool": tool,
            "generated_at": "2026-08-17T06:05:00+08:00",
            "image_version": EDA_VERSION, "ahead": 0,
            "behind_commits": 0, "pin_kind": "pin", "integrated": True,
            "pinned_ref": "c" * 40, "pinned_ref_full": "c" * 40,
            "sync_lag": 0, "release_lag": 0,
            "ours_unshipped": 0, "ours_unshipped_substantive": 0,
        }), encoding="utf-8")
    monkeypatch.setattr(PAGE, "STATE", state)
    monkeypatch.setattr(PAGE, "LEDGER", ledger)
    monkeypatch.setattr(PAGE, "REPORTS", state / "reports")
    monkeypatch.setattr(PAGE, "RELEASE_STATE", state / "daily-release.json")
    monkeypatch.setattr(
        PAGE.inventory, "collect", lambda _image: {"a": [{"dir": "tool"}]})
    monkeypatch.setattr(PAGE, "_image_ref", lambda: "fixture/image:0")
    return state


def test_strict_zero_round_keeps_github_truth_without_an_internal_status_card(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    _page_state(tmp_path, monkeypatch)
    out = tmp_path / "site" / "eda-forks.html"
    out.parent.mkdir()

    PAGE.build(out, publication=evidence)

    html = out.read_text(encoding="utf-8")
    assert '"open_delivery_count": 0' in html
    assert '"status": "converged"' in html
    assert '"converged": true' in html
    assert '"round_converged": true' in html
    assert "Open delivery PRs · GitHub" not in html
    assert "未完成交付 PR · GitHub" not in html
    assert '{en:"Last check time", zh:"最後檢查時間", wide:true}' in html
    assert ".fork-kpi.is-wide{grid-column:span 2}" in html
    # A KPI value never wraps ("2026-09-28" / "05:41", "v0.3.8" / "4"); it
    # shrinks to fit its card instead.
    assert "word-break:break-all" not in html
    assert "color:#f5f8fb;white-space:nowrap}" in html
    assert "function fitKpiNumbers()" in html
    assert "  fitKpiNumbers();\n" in html
    assert "The upstream-gap card is a live follow-up measurement" not in html
    assert "上游落後數是即時追蹤值" not in html
    assert "Last Gatekeeper run:" not in html
    assert "最後 Gatekeeper 執行：" not in html
    assert "Release record did not reach a terminal state" not in html
    assert "Release 紀錄沒有抵達終態" not in html
    assert "The 05:30 process stopped while preparing" not in html
    assert "05:30 流程在準備" not in html
    assert "Whole 05:30 round converged" in html
    assert not list(out.parent.glob(".eda-forks.html.*"))


def _render_ids(page: Path, *ids: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the emitted page")
    env = dict(os.environ, RENDER_IDS=",".join(ids))
    return subprocess.run(
        [node, str(HERE / "jstests" / "render_page.mjs"), str(page),
         FIXTURE_NOW.isoformat()],
        capture_output=True, text=True, errors="replace", env=env, timeout=30,
    )


def test_certified_page_displays_the_exact_gap_bound_full_pin(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    tool = GH_FIX.REGISTERED_FORKS[0]
    ledger_path = state / "ledger" / f"{tool}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    full_pin = ledger["pinned_ref_full"]
    ledger["pinned_ref"] = "deadbeefdead"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "eda-forks.html"

    PAGE.build(out, publication=evidence)
    rendered = _render_ids(out, "forkRows")

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert full_pin in rendered.stdout
    assert "deadbeefdead" not in rendered.stdout


def test_certified_page_rejects_a_wrong_full_pin_even_if_an_alias_matches(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    tool = GH_FIX.REGISTERED_FORKS[0]
    ledger_path = state / "ledger" / f"{tool}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["pinned_ref"] = "c" * 40
    ledger["pinned_ref_full"] = "d" * 40
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="pinned_ref_full differs"):
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_embedded_json_cannot_end_script_or_trigger_later_template_expansion(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    tool = GH_FIX.REGISTERED_FORKS[0]
    ledger_path = state / "ledger" / f"{tool}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["role"] = (
        '</script><script>globalThis.pageOwned=true</script>'
        '\u2028__RELEASE__&payload')
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "eda-forks.html"

    PAGE.build(out, publication=evidence)
    html = out.read_text(encoding="utf-8")
    rendered = _render_ids(out, "forkMetrics")

    assert "</script><script>globalThis.pageOwned" not in html
    assert r"\u003c/script\u003e\u003cscript\u003e" in html
    assert r"\u2028__RELEASE__\u0026payload" in html
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert "ReferenceError" not in rendered.stdout


def test_certified_page_uses_its_round_not_an_unbound_report(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    reports = state / "reports"
    reports.mkdir()
    # Malformed and stale: a certified build must not even consume it.
    (reports / "2099-01-01.json").write_text(
        '{"date":"STALE-UNBOUND",', encoding="utf-8")
    out = tmp_path / "eda-forks.html"

    PAGE.build(out, publication=evidence)
    html = out.read_text(encoding="utf-8")

    assert "STALE-UNBOUND" not in html
    assert FIXTURE_NOW.isoformat() in html
    assert '"run_id": "' + "a" * 64 + '"' in html


def test_whole_certificate_expires_against_an_injected_clock(tmp_path):
    paths = _strict_fixture(tmp_path)
    boundary = datetime.fromisoformat(GH_FIX.ROUND_AT)

    _load_strict(paths, now=boundary + PAGE.PUBLICATION_MAX_AGE)
    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="certificate is older than 30 hours"):
        _load_strict(
            paths,
            now=boundary + PAGE.PUBLICATION_MAX_AGE
            + timedelta(microseconds=1),
        )


def test_certificate_git_reads_ignore_ambient_git_authority(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    hostile = {
        "GIT_DIR": str(tmp_path / "other.git"),
        "GIT_WORK_TREE": str(tmp_path / "other-worktree"),
        "GIT_INDEX_FILE": str(tmp_path / "other-index"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path / "alternates"),
        "GIT_REPLACE_REF_BASE": "refs/hostile/replace/",
        "GIT_NAMESPACE": "hostile",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "hooks"),
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    real_run = subprocess.run
    git_calls: list[tuple[list[str], dict[str, str]]] = []

    def recording_run(argv, *args, **kwargs):
        if argv and argv[0] == "/usr/bin/git":
            git_calls.append((list(argv), dict(kwargs.get("env") or {})))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(LL.subprocess, "run", recording_run)

    _load_strict(paths)

    assert len(git_calls) == 5
    for argv, env in git_calls:
        assert argv[:2] == ["/usr/bin/git", "--no-replace-objects"]
        assert "core.hooksPath=/dev/null" in argv
        assert all(env.get(name) != value for name, value in hostile.items())
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_fully_proved_published_release_can_authorize_the_page(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    prior = paths["ll_record"]
    published = AI_FIX._published_release_record(EDA_VERSION)
    published.update(
        started_at="2026-08-17T06:03:30+08:00",
        finished_at="2026-08-17T06:04:00+08:00",
    )
    published["release_record_publication"]["identity"] = \
        RELEASE_RECORD_COMMIT
    replacement = LL.make_record(
        published, paths["gap_record"], GH_FIX.ROUND_AT, 0, [],
        semantic_pytest=prior["selftest"], tick_state="ran",
        fork_github=paths["github_record"],
        ai_authorization=prior["ai_authorization"],
        release_pins=prior["release_pins"],
        watch_reset=prior["watch_only_reset"],
    )
    replacement["captured_at"] = FIXTURE_NOW.isoformat()
    paths["ll"].write_text(json.dumps(replacement), encoding="utf-8")
    evidence = _load_strict(paths)
    _page_state(tmp_path, monkeypatch)
    out = tmp_path / "published.html"

    PAGE.build(out, publication=evidence)

    assert evidence.ll["release"]["status"] == "published"
    assert '"status": "published"' in out.read_text(encoding="utf-8")


def test_changed_ledger_measurement_cannot_wear_the_zero_gap_certificate(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    changed = state / "ledger" / f"{GH_FIX.REGISTERED_FORKS[0]}.json"
    row = json.loads(changed.read_text(encoding="utf-8"))
    row["sync_lag"] = 999
    changed.write_text(json.dumps(row), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError,
                       match=r"sync_lag differs from fork-gap"):
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


@pytest.mark.parametrize("field,value,needle", [
    pytest.param("generated_at", "2026-08-17T06:03:45+08:00",
                 "predates release/pin completion", id="pre-release-ledger"),
    pytest.param("image_version", "9.9.9",
                 "differs from release target", id="wrong-image-version"),
    pytest.param("sync_lag", False,
                 "sync_lag differs from fork-gap", id="boolean-zero"),
])
def test_ledger_from_another_release_cannot_replace_the_last_good_page(
        tmp_path, monkeypatch, field, value, needle):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    changed = state / "ledger" / f"{GH_FIX.REGISTERED_FORKS[0]}.json"
    row = json.loads(changed.read_text(encoding="utf-8"))
    row[field] = value
    changed.write_text(json.dumps(row), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError, match=needle):
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def _set_nested(value: dict, path: tuple[str, ...], replacement) -> None:
    target = value
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = replacement


@pytest.mark.parametrize("path,replacement", [
    pytest.param(("release", "tick_exit"), 1, id="tick-exit"),
    pytest.param(("release", "tick_exit"), False, id="boolean-tick-exit"),
    pytest.param(("release", "schema_version"), 2.0,
                 id="float-release-schema"),
    pytest.param(("release", "tick_state"), "not_checked_pytest_red",
                 id="tick-state"),
    pytest.param(("release", "anchor_pr", "ok"), False,
                 id="noop-anchor"),
    pytest.param(("watch_only_reset", "status"), "blocked",
                 id="watch-reset-status"),
    pytest.param(("watch_only_reset", "run_id"), "b" * 64,
                 id="watch-reset-run"),
    pytest.param(("release_pins", "revision"), "0" * 40,
                 id="release-pin-revision"),
    pytest.param(("release_pins", "outstanding"), ["yosys"],
                 id="release-pin-outstanding"),
    pytest.param(("selftest", "elapsed_seconds"), "not-a-number",
                 id="selftest-elapsed"),
    pytest.param(("selftest", "progress", "stage"), "never-ran",
                 id="selftest-progress"),
    pytest.param(("selftest", "progress", "finished"), True,
                 id="boolean-selftest-finished"),
    pytest.param(("release", "remote_publication", "verified"), False,
                 id="remote-publication"),
    pytest.param(("release", "release_record_publication", "identity"),
                 "0" * 40, id="release-record-identity"),
    pytest.param(("convergence", "live_sync_lag"), False,
                 id="boolean-convergence-zero"),
])
def test_mutated_ll_terminal_cannot_replace_the_last_good_page(
        tmp_path, path, replacement):
    paths = _strict_fixture(tmp_path)
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    _set_nested(ll, path, replacement)
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError):
        evidence = _load_strict(paths)
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


@pytest.mark.parametrize("with_failure", [False, True])
def test_a_published_status_without_published_proofs_is_not_green(
        tmp_path, with_failure):
    paths = _strict_fixture(tmp_path)
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    release = ll["release"]
    release.update(status="published", noop=False)
    if with_failure:
        release["failure"] = {"category": "push_failed"}
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError):
        _load_strict(paths)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


@pytest.mark.parametrize("github", [
    pytest.param(
        lambda: GH_FIX._fork_record(
            status="open", items=[GH_FIX._delivery_item()]),
        id="open-delivery",
    ),
    pytest.param(
        _unmeasured_github,
        id="unmeasured",
    ),
])
def test_open_or_unmeasured_github_cannot_replace_the_page(
        tmp_path, github):
    paths = _strict_fixture(tmp_path, github=github())
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError):
        evidence = _load_strict(paths)
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_gap_and_github_run_id_mismatch_cannot_replace_the_page(tmp_path):
    github = GH_FIX._fork_record()
    github["round"]["run_id"] = "b" * 64
    paths = _strict_fixture(tmp_path, github=github)
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError, match="run_id"):
        _load_strict(paths)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_same_nonce_with_different_round_times_cannot_replace_the_page(tmp_path):
    paths = _strict_fixture(tmp_path)
    github = json.loads(paths["github"].read_text(encoding="utf-8"))
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    github["round"]["finished_at"] = "2026-08-17T05:58:00+08:00"
    ll["fork_github"]["round"]["finished_at"] = \
        "2026-08-17T05:58:00+08:00"
    paths["github"].write_text(json.dumps(github), encoding="utf-8")
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="different daily_0530 round finished_at"):
        _load_strict(paths)


@pytest.mark.parametrize("mutation,needle", [
    # #78 — a certified page is a RAN tick, so the record on disk is run_tick's
    # re-verification: the out-of-order shape is an authorization OLDER than the
    # gate run it vouches for, not newer. See `capture_release_ll`.
    pytest.param(
        lambda _github, ll: ll["selftest"].update(
            started_at="2026-08-17T06:03:00+08:00"),
        "AI authorization predates the semantic pytest run it authorized",
        id="ai-predates-selftest"),
    pytest.param(
        lambda _github, ll: ll["release_pins"].update(
            checked_at="2026-08-17T06:03:45+08:00"),
        "release pins were checked before the release finished",
        id="pins-before-release-finish"),
    pytest.param(
        lambda github, ll: (
            github.update(started_at="2026-08-17T06:06:30+08:00"),
            ll["fork_github"].update(
                started_at="2026-08-17T06:06:30+08:00")),
        "GitHub convergence started before fork-gap collection finished",
        id="github-before-gap-finish"),
])
def test_out_of_order_current_records_cannot_form_a_page_certificate(
        tmp_path, mutation, needle):
    paths = _strict_fixture(tmp_path)
    github = json.loads(paths["github"].read_text(encoding="utf-8"))
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    mutation(github, ll)
    paths["github"].write_text(json.dumps(github), encoding="utf-8")
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError, match=needle):
        _load_strict(paths)


def test_valid_ancestor_cannot_impersonate_current_release_record(tmp_path):
    paths = _strict_fixture(tmp_path)
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    assert ANCESTOR_COMMIT != RELEASE_RECORD_COMMIT
    ll["release"]["release_record_publication"]["identity"] = ANCESTOR_COMMIT
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="current RELEASED.json commit"):
        _load_strict(paths)


def test_self_consistent_wrong_release_target_cannot_override_checkout(tmp_path):
    paths = _strict_fixture(tmp_path)
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    ll["release"]["target"] = "9.9.9"
    ll["release"]["version"] = {"from": "9.9.9", "to": "9.9.9"}
    ll["release"]["remote_publication"]["manifests"]["version"]["ref"] = \
        "ghcr.io/vibeic/" + "vibeic-eda:" + "9.9.9"
    ll["release_pins"]["version"] = "9.9.9"
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="VERSION/RELEASED.json"):
        _load_strict(paths)


@pytest.mark.parametrize("raw,needle", [
    ('{"program":"fork_gap_report","program":"fork_gap_report"}',
     "duplicate JSON key"),
    ('{"program":"fork_gap_report","value":NaN}',
     "non-finite JSON number"),
    ('{"program":"fork_gap_report","value":1e999}',
     "non-finite JSON number"),
])
def test_duplicate_or_nonfinite_json_is_refused_before_page_replacement(
        tmp_path, raw, needle):
    paths = _strict_fixture(tmp_path)
    paths["gap"].write_text(raw, encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    with pytest.raises(PAGE.PublicationEvidenceError, match=needle):
        _load_strict(paths)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_atomic_page_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    def refuse_replace(_source, _target):
        raise OSError("fixture rename refusal")

    monkeypatch.setattr(PAGE.os, "replace", refuse_replace)
    with pytest.raises(OSError, match="fixture rename refusal"):
        PAGE._atomic_page(out, "NEW PAGE")

    assert out.read_text(encoding="utf-8") == "LAST GOOD"
    assert not list(tmp_path.glob(".eda-forks.html.*"))


# --- #79: the round is judged where the page says it is ----------------------

def _upstream_moved_after_the_merge(paths: dict) -> None:
    """What every real round looks like by the time the gap is measured.

    The image takes ~45 minutes to compose and upstream does not wait. On the
    round of 2026-08-21 the release landed at 12:01 and the gap was measured at
    12:06 with the live pair already at 2, while both at-merge figures were 0.
    """
    gap = json.loads(paths["gap"].read_text())
    # The totals are cross-checked against the retained per-fork rows, so the
    # drift has to be expressed where it actually happens: on one fork.
    gap["rows"][0]["image_behind"] = 2
    gap["rows"][0]["sync_lag"] = 2
    gap["q1_image_behind_upstream"] = 2
    gap["q1_sync_lag"] = 2
    gap["q1_forks_behind"] = 1
    paths["gap"].write_text(json.dumps(gap))
    ll = json.loads(paths["ll"].read_text())
    ll["convergence"].update({
        "live_sync_lag": 2, "image_behind_upstream": 2,
        "delivery_converged": False, "converged": False,
    })
    paths["ll"].write_text(json.dumps(ll))


def test_upstream_moving_during_the_build_does_not_block_the_page(tmp_path):
    """THE RACE NO ROUND CAN WIN. The gate demanded the LIVE gap be zero while
    the page's own caption says the round is judged at its merge boundary and
    its renderer derives convergence from `q1_sync_lag_at_merge` under the
    comment "the live sum can essentially never read 0". The page had not
    published for 12 consecutive rounds."""
    paths = _strict_fixture(tmp_path)
    _upstream_moved_after_the_merge(paths)

    evidence = _load_strict(paths)          # must not raise

    assert evidence is not None


def test_a_gap_that_was_never_closed_still_blocks_the_page(tmp_path):
    """THE DIRECTION THAT MAKES THE SWITCH SAFE. Judging at the merge boundary
    is not the same as not judging: a round that did NOT take everything it saw
    is still refused."""
    paths = _strict_fixture(tmp_path)
    gap = json.loads(paths["gap"].read_text())
    gap["q1_image_behind_upstream_at_merge"] = 3
    paths["gap"].write_text(json.dumps(gap))

    with pytest.raises(PAGE.PublicationEvidenceError) as caught:
        _load_strict(paths)
    assert "q1_image_behind_upstream_at_merge" in str(caught.value)


@pytest.mark.parametrize("field", [
    "q1_unmeasured", "q1_unmeasured_at_merge",
    "q1_unmeasured_image_behind_at_merge", "q2_unmeasured_ship",
])
def test_a_tool_that_could_not_be_measured_is_not_a_zero(tmp_path, field):
    """ABSENT IS NOT ZERO. Both at-merge sums add `value or 0`, so an
    unmeasurable tool contributes nothing and reads as converged. Without this
    the switch to the at-merge figures would trade a race for a blind spot."""
    paths = _strict_fixture(tmp_path)
    gap = json.loads(paths["gap"].read_text())
    gap[field] = ["some-tool"]
    paths["gap"].write_text(json.dumps(gap))

    with pytest.raises(PAGE.PublicationEvidenceError) as caught:
        _load_strict(paths)
    assert field in str(caught.value)
