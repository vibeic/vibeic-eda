"""Branch-list request failures are missing evidence, never zero branches."""
from __future__ import annotations

import json
import time

import pytest

import check_pins_current as C


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize("error", [
    "unexpected end of JSON input",
    "Get https://api.github.com: unexpected EOF",
    "context deadline exceeded",
    "Connection reset by peer",
    "Could not resolve host: api.github.com",
    "HTTP 502: Bad gateway",
    "HTTP 429: API rate limit exceeded",
    "net/http: TLS handshake timeout",
])
def test_truncated_branch_get_is_reasked_without_using_partial_rows(
        monkeypatch, error):
    calls = []
    sha = "a" * 40

    def answer(cmd, timeout=180):
        calls.append((list(cmd), timeout))
        if len(calls) == 1:
            return 1, "wrong " + "b" * 40, error
        return 0, f"main {sha}\n", ""

    monkeypatch.setattr(C, "_sh", answer)
    lookup = C._branches("yosys")
    assert lookup.outcome == C.BRANCH_LOOKUP_OK
    assert lookup.branches == (("main", sha),)
    assert len(calls) == 2
    assert calls[0] == calls[1], "a retry must ask the same question"
    assert calls[0][1] == 180
    assert calls[0][0][-2:] == ["--method", "GET"]


@pytest.mark.parametrize("rc, output, error", [
    (0, "main malformed\n", "unexpected end of JSON input"),
    (1, "", "HTTP 404: Not Found"),
    (1, "", "HTTP 403: Resource not accessible by integration"),
    (1, "", "HTTP 401: Bad credentials"),
    (1, "", "HTTP 404: Not Found; unexpected EOF"),
    (1, "", "HTTP/2 403: Resource not accessible; unexpected EOF"),
    (1, "", "HTTP 400: Bad Request; unexpected EOF"),
    (1, "", "unknown flag: --bad"),
])
def test_answered_or_nontransport_branch_failure_is_not_shopped_for_green(
        monkeypatch, rc, output, error):
    calls = []

    def answer(cmd, timeout=180):
        calls.append(list(cmd))
        return rc, output, error

    monkeypatch.setattr(C, "_sh", answer)
    lookup = C._branches("yosys")
    assert lookup.outcome == C.BRANCH_LOOKUP_UNKNOWN
    assert len(calls) == 1


@pytest.mark.parametrize("fails_at", ["branches", "compare"])
@pytest.mark.parametrize("recovers", [False, True])
def test_real_pin_comparison_recovers_or_remains_unmeasured(
        monkeypatch, capsys, fails_at, recovers):
    # Read the actual in-repo branch declaration, not a second convention.
    branch = C.declared_branch("yosys")
    assert branch
    pin = "a" * 40
    attempts = []

    def answer(cmd, timeout=180):
        path = next(arg for arg in cmd if arg.startswith("repos/"))
        is_branch = "/branches?" in path
        is_failure_target = is_branch == (fails_at == "branches")
        payload = (f"{branch} {pin}\n" if is_branch
                   else json.dumps({"status": "identical", "total_commits": 0}))
        if is_failure_target:
            attempts.append((list(cmd), timeout))
            if not recovers or len(attempts) == 1:
                # Even complete-looking output on rc1 is not evidence.
                return 1, payload, "unexpected end of JSON input"
        return 0, payload, ""

    monkeypatch.setattr(C, "_sh", answer)
    result = C.check_one("yosys", pin)
    expected = ("CURRENT" if recovers else
                "BRANCH_LOOKUP_UNKNOWN" if fails_at == "branches"
                else "COMPARE_FAILED")
    assert result["verdict"] == expected
    assert len(attempts) == (2 if recovers else 3)
    assert all(attempt == attempts[0] for attempt in attempts)
    if not recovers:
        diagnostic = capsys.readouterr().err + result["detail"]
        for number in (1, 2, 3):
            assert f"attempt {number}/3" in diagnostic


def test_rc_zero_malformed_compare_is_not_retried(monkeypatch):
    branch = C.declared_branch("yosys")
    calls = []

    def answer(cmd, timeout=180):
        path = next(arg for arg in cmd if arg.startswith("repos/"))
        if "/branches?" in path:
            return 0, f"{branch} " + "a" * 40, ""
        calls.append(list(cmd))
        return 0, "{malformed", ""

    monkeypatch.setattr(C, "_sh", answer)
    assert C.check_one("yosys", "a" * 40)["verdict"] == "COMPARE_FAILED"
    assert len(calls) == 1


def test_ownership_reads_use_the_same_bounded_get_retry(monkeypatch):
    counts = {}

    def answer(cmd, timeout=180):
        path = next(arg for arg in cmd if arg.startswith("repos/"))
        counts[path] = counts.get(path, 0) + 1
        if counts[path] == 1:
            return 1, "", "unexpected end of JSON input"
        return (0, "4", "") if "/compare/" in path else (0, "up/repo", "")

    monkeypatch.setattr(C, "_sh", answer)
    assert C.branch_is_ours("yosys", "main") is True
    assert sorted(counts.values()) == [2, 2]


def test_branch_request_deadline_is_explicit_unknown(monkeypatch):
    monkeypatch.setattr(
        C, "_sh",
        lambda *a, **k: (
            124, "main deadbeef\n", "REQUEST_DEADLINE: GitHub did not answer",
        ),
    )

    lookup = C._branches("yosys")
    assert lookup.outcome == C.BRANCH_LOOKUP_UNKNOWN
    assert lookup.branches == (), "partial output must not become a branch list"

    verdict = C.check_one("yosys", "a" * 40)
    assert verdict["verdict"] == "BRANCH_LOOKUP_UNKNOWN"
    assert "not NO_BRANCHES" in verdict["detail"]
    assert verdict["measurement"] == {
        "kind": "github_branch_list",
        "outcome": "BRANCH_LOOKUP_UNKNOWN",
        "exit_code": 124,
    }


def test_successful_empty_branch_response_is_the_only_no_branches(monkeypatch):
    monkeypatch.setattr(C, "_sh", lambda *a, **k: (0, "", ""))

    lookup = C._branches("empty-repository")
    assert lookup.outcome == C.BRANCH_LOOKUP_OK
    assert lookup.branches == ()
    assert C.check_one("empty-repository", "b" * 40)["verdict"] == "NO_BRANCHES"


def test_successful_well_formed_branch_response_is_preserved(monkeypatch):
    sha = "d" * 40
    monkeypatch.setattr(C, "_sh", lambda *a, **k: (0, f"main {sha}\n", ""))
    lookup = C._branches("yosys")
    assert lookup.outcome == C.BRANCH_LOOKUP_OK
    assert lookup.branches == (("main", sha),)


def test_malformed_branch_rows_are_unknown_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(
        C, "_sh", lambda *a, **k: (0, "main deadbeef\nbroken-row\n", ""),
    )
    lookup = C._branches("yosys")
    assert lookup.outcome == C.BRANCH_LOOKUP_UNKNOWN
    assert lookup.branches == ()
    assert "malformed" in lookup.detail


def test_branch_lookup_unknown_exits_two_and_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "pinned_refs", lambda _root: {"yosys": "a" * 40})
    monkeypatch.setattr(C, "check_one", lambda repo, pin: {
        "repo": repo,
        "pin": pin[:9],
        "verdict": "BRANCH_LOOKUP_UNKNOWN",
        "detail": "branch API did not answer",
        "measurement": {"outcome": "BRANCH_LOOKUP_UNKNOWN", "exit_code": 124},
    })
    record = tmp_path / "pins.json"

    rc = C.main(["--eda-root", str(tmp_path), "--json", str(record)])
    saved = json.loads(record.read_text())

    assert rc == C.RC_NOTHING
    assert saved["unmeasured"][0]["verdict"] == "BRANCH_LOOKUP_UNKNOWN"
    assert saved["commits_not_shipping"] == 0


def test_stale_with_unknown_ownership_is_also_not_checked(
        monkeypatch, tmp_path):
    monkeypatch.setattr(C, "pinned_refs", lambda _root: {"yosys": "a" * 40})
    monkeypatch.setattr(C, "check_one", lambda repo, pin: {
        "repo": repo,
        "pin": pin[:9],
        "branch": "vibeic/integration",
        "verdict": "STALE_UNDECIDED",
        "behind": 3,
        "ours": None,
        "detail": "ownership could not be determined",
    })

    assert C.main(["--eda-root", str(tmp_path)]) == C.RC_NOTHING
