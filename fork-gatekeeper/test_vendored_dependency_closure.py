from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import vendored_dependency_closure as C


def _git(repo: Path, *args: str, env=None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True, env=env,
    )
    return proc.stdout.strip()


def _init_work(path: Path, branch: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    _git(path, "config", "user.name", "Test Author")
    _git(path, "config", "user.email", "test@example.com")


def _commit(path: Path, name: str, content: str, message: str) -> str:
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(content, encoding="utf-8")
    _git(path, "add", "--", name)
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _bare_from(work: Path, bare: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")


def _fixture(tmp_path: Path, *, check_ok: bool = True):
    forks = tmp_path / "forks"
    forks.mkdir()

    nested_work = tmp_path / "nested-work"
    _init_work(nested_work, "main")
    old = _commit(nested_work, "value.txt", "old\n", "old nested value")
    new = _commit(nested_work, "value.txt", "new\n", "new nested value")
    nested_bare = tmp_path / "Nested.git"
    _bare_from(nested_work, nested_bare, "main")
    subprocess.run(["git", "clone", "-q", str(nested_bare),
                    str(forks / "Nested")], check=True)

    host_work = tmp_path / "host-work"
    _init_work(host_work, "master")
    (host_work / ".gitmodules").write_text(
        '[submodule "nested"]\n'
        '  path = vendor/nested\n'
        '  url = https://github.com/vibeic/Nested.git\n',
        encoding="utf-8",
    )
    (host_work / "check.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8")
    _git(host_work, "add", ".gitmodules", "check.py")
    _git(host_work, "update-index", "--add", "--cacheinfo",
         f"160000,{old},vendor/nested")
    _git(host_work, "commit", "-q", "-m", "pin old nested value")
    host_old = _git(host_work, "rev-parse", "HEAD")
    host_bare = tmp_path / "Host.git"
    _bare_from(host_work, host_bare, "master")
    subprocess.run(["git", "clone", "-q", str(host_bare),
                    str(forks / "Host")], check=True)
    _git(forks / "Host", "config",
         f"url.{nested_bare.as_uri()}.insteadOf",
         "https://github.com/vibeic/Nested.git")

    eda = tmp_path / "eda"
    (eda / "tools" / "host").mkdir(parents=True)
    (eda / "tools" / "host" / "Dockerfile").write_text(
        f"ARG HOST_REF={host_old} # pinned; branch master\n"
        "ARG HOST_REPO=https://github.com/vibeic/Host.git\n"
        "RUN git clone \"${HOST_REPO}\" /host \\\n"
        " && cd /host && git checkout ${HOST_REF} \\\n"
        " && git submodule update --init --recursive\n",
        encoding="utf-8",
    )
    registry = {
        "forks": [
            {
                "tool": "Host", "branch": "master", "upstream": "up/Host",
                "post_merge_check": [{
                    "name": "candidate-check", "path": "check.py",
                    "cmd": (["python3", "check.py"] if check_ok else
                            ["python3", "-c", "raise SystemExit(7)"]),
                }],
            },
            {"tool": "Nested", "branch": "main", "upstream": "up/Nested"},
        ]
    }
    forks_json = eda / "fork-gatekeeper" / "FORKS.json"
    forks_json.parent.mkdir(parents=True)
    forks_json.write_text(json.dumps(registry), encoding="utf-8")
    return eda, forks, forks_json, host_bare, host_old, old, new


def test_stale_vendored_gitlink_advances_host_and_reaches_fixed_point(
        tmp_path, monkeypatch):
    eda, forks, registry, host_bare, host_old, old, new = _fixture(tmp_path)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")

    rc, record = C.run(
        eda, forks, registry, dry_run=False, max_depth=3, max_passes=5)

    assert rc == C.RC_OK and record["status"] == "complete"
    assert record["updated_hosts"] == ["Host"]
    update = record["passes"][0]["updates"][0]
    assert update["state"] == "UPDATED"
    assert update["from"] == host_old and update["to"] != host_old
    assert [row["ok"] for row in update["checks"]] == [True]
    remote_tip = _git(host_bare, "rev-parse", "refs/heads/master")
    assert remote_tip == update["to"]
    assert _git(host_bare, "ls-tree", remote_tip, "vendor/nested").split()[2] == new
    body = _git(host_bare, "show", "-s", "--format=%B", remote_tip)
    assert "Signed-off-by: vibeic-fork-gatekeeper" in body
    assert record["passes"][-1]["edges"][0]["relation"] == "current"


def test_failed_host_check_blocks_publication(tmp_path, monkeypatch):
    eda, forks, registry, host_bare, host_old, old, new = _fixture(
        tmp_path, check_ok=False)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")

    rc, record = C.run(
        eda, forks, registry, dry_run=False, max_depth=3, max_passes=5)

    assert rc == C.RC_NEEDS_HUMAN and record["status"] == "needs_human"
    update = record["passes"][0]["updates"][0]
    assert update["state"] == "CHECK_FAILED"
    assert _git(host_bare, "rev-parse", "refs/heads/master") == host_old


def test_current_graph_is_a_noop(tmp_path, monkeypatch):
    eda, forks, registry, host_bare, host_old, old, new = _fixture(tmp_path)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")
    rc, first = C.run(
        eda, forks, registry, dry_run=False, max_depth=3, max_passes=5)
    assert rc == C.RC_OK
    first_tip = _git(host_bare, "rev-parse", "refs/heads/master")

    rc, second = C.run(
        eda, forks, registry, dry_run=False, max_depth=3, max_passes=5)

    assert rc == C.RC_OK and second["status"] == "complete"
    assert second["updated_hosts"] == []
    assert second["passes"][0]["updates"] == []
    assert _git(host_bare, "rev-parse", "refs/heads/master") == first_tip
