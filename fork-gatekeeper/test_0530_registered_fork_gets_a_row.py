#!/usr/bin/env python3
"""A fork registered in FORKS.json gets a row in the 05:30 round, checkout or not.

Measured 2026-09-27 05:30: cace, eqy and spicebind had been registered the day
before (vibeic-eda #243/#244/#245) but never cloned under the forks root.  The
round walked the directory listing, so it produced 39 rows for 42 registered
forks and said nothing; the release authorizer then refused the round 15
minutes later with "missing top-level row(s): cace, eqy, spicebind" and no
release was attempted.

The round must either bring a newly registered fork into the walk (clone it
and treat it like every other fork) or fail at the START naming it -- a row
that says why, never no row.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


HERE = Path(__file__).resolve().parent


def _load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


D = _load("daily_0530", "d0530_registered_fork_row")
A = _load("check_ai_release_authorization", "ai_auth_registered_fork_row")

TOOL = "newtool"
_EVIDENCE = {"outcome": "complete", "upstream_repository": f"owner/{TOOL}",
             "fork_owner": "vibeic", "open_count": 0, "items": []}


def _git(*args: str, cwd: Path | None = None) -> str:
    cp = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                         *args], cwd=cwd, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr or cp.stdout
    return cp.stdout.strip()


def _stage(tmp_path: Path, monkeypatch, *, clonable: bool = True) -> dict:
    """A registry with one fork, bare origin/upstream repos, and an EMPTY forks
    root: the fork is registered but was never cloned."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "a").write_text("a\n")
    _git("-C", str(seed), "add", "a")
    _git("-C", str(seed), "commit", "-q", "-m", "a")
    origins = tmp_path / "origins"
    upstreams = tmp_path / "upstreams"
    origin = origins / f"{TOOL}.git"
    upstream = upstreams / "owner" / f"{TOOL}.git"
    for bare in (origin, upstream):
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "-q", "--bare", "-b", "main", str(bare))
        _git("-C", str(seed), "push", "-q", str(bare), "main")
    if not clonable:
        subprocess.run(["rm", "-rf", str(origin)], check=True)

    registry = tmp_path / "FORKS.json"
    registry.write_text(json.dumps({"forks": [
        {"tool": TOOL, "branch": "main", "upstream": f"owner/{TOOL}"}]}))
    forks = tmp_path / "forks"
    forks.mkdir()

    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "FORKS_JSON", registry)
    monkeypatch.setattr(D, "FORK_CLONE_URL", str(origins) + "/{tool}.git")
    monkeypatch.setattr(D, "UPSTREAM_CLONE_URL", str(upstreams) + "/{slug}.git")
    monkeypatch.setattr(D, "EDA", tmp_path / "no-such-eda-checkout")
    monkeypatch.setattr(D, "image_pins", lambda *a, **k: {})
    monkeypatch.setattr(D, "post_merge_checks", lambda repo: [])
    monkeypatch.setattr(D, "_upstream_pull_request_evidence",
                        lambda g: dict(_EVIDENCE))
    monkeypatch.setattr(D, "step4_prune",
                        lambda g, main, rep, apply: rep.setdefault("pruned", []))
    monkeypatch.setattr(
        D, "step2b_ai_decisions",
        lambda *a, **k: {"cases": 0, "invoked": False, "verified": True,
                         "cleared": True, "verified_cases": 0,
                         "verification": [], "unverified_cases": []})
    monkeypatch.setattr(D.round_record, "write",
                        lambda *a, **k: {"stamp": "test", "rows": 1})
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path / "state"))
    return {"forks": forks, "registry": registry, "upstream": upstream,
            "json": tmp_path / "daily_0530.json"}


def _authorizer_errors(record: dict) -> list[str]:
    _keys, errors = A._conflicted_cases(record, [TOOL])
    return errors


def test_registered_fork_without_a_checkout_is_cloned_and_walked(
        tmp_path, monkeypatch):
    s = _stage(tmp_path, monkeypatch)
    rc = D.main_(["--apply", "--no-ai", "--skip-build", "--json",
                  str(s["json"])])
    record = json.loads(s["json"].read_text())

    clone = s["forks"] / TOOL
    assert (clone / ".git").is_dir(), "the registered fork was never cloned"
    assert _git("-C", str(clone), "remote", "get-url", "upstream") == \
        str(s["upstream"]), "the declared upstream was not configured"
    row = record.get(TOOL)
    assert isinstance(row, dict), f"no row for registered fork: {sorted(record)}"
    assert "error" not in row, row
    assert row["main"] == "main"
    assert row["upstream_merge_outcome"] == "natural_success", row
    assert _authorizer_errors(record) == []
    assert rc == 0


def test_unclonable_registered_fork_fails_loudly_with_a_named_row(
        tmp_path, monkeypatch, capsys):
    s = _stage(tmp_path, monkeypatch, clonable=False)
    rc = D.main_(["--apply", "--no-ai", "--skip-build", "--json",
                  str(s["json"])])
    record = json.loads(s["json"].read_text())

    assert rc == 1, "a registered fork the round could not walk read as clean"
    row = record.get(TOOL)
    assert isinstance(row, dict), "an unclonable fork vanished from the record"
    assert row["needs_human"] is True
    assert "registered in FORKS.json" in row["error"]
    assert "clone failed" in row["error"]
    errors = _authorizer_errors(record)
    assert not any("missing top-level" in e for e in errors), errors
    assert any(e.startswith(f"{TOOL}.error") for e in errors), errors
    assert f"registered fork {TOOL} has no usable checkout" in \
        capsys.readouterr().err


def test_dry_run_names_the_missing_checkout_and_clones_nothing(
        tmp_path, monkeypatch):
    s = _stage(tmp_path, monkeypatch)
    rc = D.main_(["--no-ai", "--skip-build", "--json", str(s["json"])])
    record = json.loads(s["json"].read_text())

    assert not (s["forks"] / TOOL).exists(), "a dry run cloned"
    assert rc == 1
    assert "no local checkout" in record[TOOL]["error"]


@pytest.mark.parametrize("registry_text", [
    "{not json", json.dumps({"forks": {}}),
    json.dumps({"forks": [{"tool": TOOL}, {"tool": TOOL}]}),
])
def test_an_unreadable_fleet_changes_nothing_and_is_norecord(
        tmp_path, monkeypatch, registry_text):
    s = _stage(tmp_path, monkeypatch)
    s["registry"].write_text(registry_text)
    assert D.main_(["--apply", "--no-ai", "--skip-build", "--json",
                    str(s["json"])]) == 2
    assert not (s["forks"] / TOOL).exists()


def test_the_production_registry_is_what_the_round_walks():
    """The round and the release authorizer must read the SAME fleet file."""
    assert D.FORKS_JSON == HERE / "FORKS.json"
    fleet, errors = D.registered_fleet()
    assert errors == []
    assert set(fleet) == set(A._registered_fleet(HERE / "FORKS.json")[0])
