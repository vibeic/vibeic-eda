"""Branch-list request failures are missing evidence, never zero branches."""
from __future__ import annotations

import json

import check_pins_current as C


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
