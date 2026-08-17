"""The branch-at-pin lookup has three outcomes, not an Optional[str].

An API failure is absence of evidence.  It must never be published as evidence
that no branch carries the pin, and it must survive into the retained JSON even
when another fork in the same round can be merged normally.
"""
import json
import pathlib
import sys


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_merge as M  # noqa: E402


PIN = "a" * 40


def test_a_transport_failure_is_unknown_not_no_branch(monkeypatch):
    monkeypatch.setattr(
        M, "_sh",
        lambda *a, **k: (127, "", "TimeoutExpired: GitHub did not answer"),
    )
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None, got
    assert "rc=127" in got.detail, got


def test_only_a_complete_listing_can_authoritatively_say_absent(monkeypatch):
    monkeypatch.setattr(
        M, "_sh",
        lambda *a, **k: (0, f"main {'b' * 40}\n", ""),
    )
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_ABSENT, got
    assert "authoritatively listed" in got.detail, got


def test_malformed_success_is_unknown_not_authoritative_absence(monkeypatch):
    monkeypatch.setattr(M, "_sh", lambda *a, **k: (0, "partial-page", ""))
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None, got


def test_unknown_is_a_named_json_row_and_blocks_the_round(tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(M, "build_branches", lambda root: {"yosys": PIN})
    monkeypatch.setattr(
        M, "branch_at_pin",
        lambda repo, pin: M.BranchLookup(
            M.BRANCH_UNKNOWN, detail="GitHub lookup did not answer", rc=127,
        ),
    )
    monkeypatch.setattr(
        M, "merge_one",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("an unresolved branch must not be merged")
        ),
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    assert rc == M.RC_NEEDS_HUMAN
    row = json.loads(record.read_text())["forks"][0]
    assert row["repo"] == "yosys"
    assert row["state"] == "BRANCH_LOOKUP_UNKNOWN", row
    assert row["branch_lookup"]["status"] == M.BRANCH_UNKNOWN, row
    assert row["branch_lookup"]["rc"] == 127, row
    assert row["state"] != "NO_BRANCH_AT_PIN", row


def test_unknown_does_not_disappear_when_another_fork_is_current(
        tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(
        M, "build_branches", lambda root: {"unknown": PIN, "healthy": PIN},
    )

    def lookup(repo, pin):
        if repo == "unknown":
            return M.BranchLookup(
                M.BRANCH_UNKNOWN, detail="transport unavailable", rc=127,
            )
        return M.BranchLookup(M.BRANCH_FOUND, branch="main", rc=0)

    monkeypatch.setattr(M, "branch_at_pin", lookup)
    monkeypatch.setattr(
        M, "merge_one",
        lambda repo, branch, dry: {
            "repo": repo, "branch": branch, "state": "ALREADY_CURRENT",
            "detail": "level with upstream/main", "conflicts": [],
            "checks": [], "took": 0,
        },
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    rows = json.loads(record.read_text())["forks"]
    assert rc == M.RC_NEEDS_HUMAN
    assert {(row["repo"], row["state"]) for row in rows} == {
        ("unknown", "BRANCH_LOOKUP_UNKNOWN"),
        ("healthy", "ALREADY_CURRENT"),
    }


def test_authoritative_absence_is_also_retained_and_blocks(
        tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(M, "build_branches", lambda root: {"yosys": PIN})
    monkeypatch.setattr(
        M, "branch_at_pin",
        lambda repo, pin: M.BranchLookup(
            M.BRANCH_ABSENT, detail="complete listing had no match", rc=0,
        ),
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    row = json.loads(record.read_text())["forks"][0]
    assert rc == M.RC_NEEDS_HUMAN
    assert row["state"] == "NO_BRANCH_AT_PIN", row
    assert row["branch_lookup"]["status"] == M.BRANCH_ABSENT, row
