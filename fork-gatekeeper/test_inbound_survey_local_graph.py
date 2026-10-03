"""Mirror comparisons use native Git and a live upstream SHA, never cached tips."""
from pathlib import Path
import subprocess

import pytest

import inbound_survey as S


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    root = tmp_path / "Mirror"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "commit", "--allow-empty", "-m", "shared root")
    base = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    pin = git(root, "commit-tree", tree, "-p", base, "-m", "fork change")
    tip = git(root, "commit-tree", tree, "-p", base, "-m", "fix upstream regression")
    git(root, "update-ref", "refs/remotes/upstream/main", base)
    git(root, "remote", "add", "upstream", "https://github.com/them/Mirror.git")
    monkeypatch.setenv("GK_FORKS_DIR", str(tmp_path))
    state = {"root": root, "pin": pin, "tip": tip, "branch": "main"}

    def api(path):
        if path == "repos/vibeic/Mirror":
            return {"fork": False}
        if path == "repos/them/Mirror":
            return {"default_branch": state["branch"]} if state["branch"] else None
        if path == "repos/them/Mirror/git/ref/heads/main":
            return {"object": {"sha": state["tip"]}} if state["tip"] else None
        return None

    monkeypatch.setattr(S, "_gh_json", api)
    return state


def survey(mirror):
    return S.survey_one("Mirror", mirror["pin"], {"Mirror": "them/Mirror"})


def test_mirror_counts_real_upstream_change_even_with_stale_tracking_ref(mirror):
    row = survey(mirror)
    assert row["state"] == "SURVEYED", row
    assert row["behind"] == 1 and row["fix_candidates"] == 1, row
    assert row["commits"][0]["sha"] == mirror["tip"][:9]
    assert row["upstream_tip"] == mirror["tip"]
    assert row["compare_scope"] == "local-graph-live-upstream"
    assert row["local_commits_invisible"] is False


def test_mirror_already_contains_live_upstream_has_measured_zero(mirror):
    mirror["tip"] = git(mirror["root"], "rev-parse", "refs/remotes/upstream/main")
    row = survey(mirror)
    assert row["state"] == "SURVEYED", row
    assert row["behind"] == 0 and row["sampled"] == 0, row


@pytest.mark.parametrize("failure", ["missing_tip", "no_api", "shallow", "wrong_remote", "unrelated"])
def test_incomplete_or_unbound_graph_is_not_measured_zero(mirror, failure):
    root = mirror["root"]
    if failure == "missing_tip":
        mirror["tip"] = "f" * 40
    elif failure == "no_api":
        mirror["tip"] = None
    elif failure == "shallow":
        (root / ".git" / "shallow").write_text(git(root, "rev-parse", "HEAD") + "\n")
    elif failure == "wrong_remote":
        git(root, "remote", "set-url", "upstream", "https://github.com/other/Mirror.git")
    else:
        tree = git(root, "rev-parse", "HEAD^{tree}")
        mirror["tip"] = git(root, "commit-tree", tree, "-m", "unrelated history")
    row = survey(mirror)
    assert row["state"] == "UNMEASURABLE", row
    assert "behind" not in row, row


def test_missing_upstream_metadata_never_guesses_default_branch(mirror):
    mirror["branch"] = None
    row = survey(mirror)
    assert row["state"] == "UNMEASURABLE", row
    assert "default branch" in row["error"]


def test_live_mirror_main_reports_measured_population(
        mirror, monkeypatch, tmp_path):
    monkeypatch.setattr(S, "pinned_refs", lambda root: {"Mirror": mirror["pin"]})
    monkeypatch.setattr(S, "declared_upstreams", lambda root: {"Mirror": "them/Mirror"})
    assert S.main(["--eda-root", str(tmp_path)]) == S.RC_OK


def test_registered_mirror_pins_are_full_source_identities():
    root = Path(__file__).resolve().parents[1]
    pins = S.pinned_refs(root)
    upstreams = S.declared_upstreams(root)
    assert pins and all(len(ref) == 40 for ref in pins.values())
    assert set(pins) <= set(upstreams)
