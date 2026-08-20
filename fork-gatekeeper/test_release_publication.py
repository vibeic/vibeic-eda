"""Publication is complete only when all three durable names agree.

The immutable version manifest, ``:latest``, and the git commit carrying
RELEASED.json are three independent publications.  These tests drive the real
``daily_release.main`` with injected read-only measurements and stubbed writers;
they never contact a registry/origin or build an image.
"""
from __future__ import annotations

from collections import deque
import fcntl
import json
import os
from pathlib import Path
import pytest
import subprocess
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_release as R  # noqa: E402
import pr_notify as P      # noqa: E402


PIN = "a" * 40


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "eda"
    root.mkdir()
    (root / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    return root


def _fingerprint() -> str:
    return R.pins_fingerprint({
        "yosys": PIN,
        "recipe:yosys": "recipe-yosys",
        "recipe:__compose__": "recipe-compose",
    })


def _stub_release_inputs(monkeypatch, root: Path, record: dict) -> None:
    # The two source guards run through subprocess directly.  All remaining
    # release I/O has its own injected stub below.
    monkeypatch.setattr(
        R.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(R, "pinned_refs", lambda _root: {"yosys": PIN})
    monkeypatch.setattr(R, "ref_arg_names", lambda _root: {"yosys": "YOSYS"})
    monkeypatch.setattr(R, "bake_targets",
                        lambda _root: {"yosys": ["YOSYS_REF"]})
    monkeypatch.setattr(R, "recipe_hash",
                        lambda _root, _target: "recipe-yosys")
    monkeypatch.setattr(R, "compose_recipe_hash",
                        lambda _root: "recipe-compose")
    monkeypatch.setattr(R, "check_one", lambda *_a: {"verdict": "CURRENT"})
    monkeypatch.setattr(R, "released_record", lambda _root: dict(record))
    monkeypatch.setattr(R, "missing_artefacts", lambda *_a: {})
    monkeypatch.setattr(R, "write_recipe_vars", lambda *_a: [])
    monkeypatch.setattr(R, "retag_images", lambda *_a: [])
    monkeypatch.setattr(R, "preferred_builder", lambda: None)
    monkeypatch.setattr(R, "log_limit_effective", lambda *_a: True)
    monkeypatch.setattr(R, "smoke_image", lambda _image: [])
    monkeypatch.setattr(R, "select_publish_version",
                        lambda *_a, **_k: ("0.2.30", "0.2.31", []))
    monkeypatch.setattr(R, "_existing_versions", lambda _root: [])
    monkeypatch.setattr(
        R, "recover_uncommitted_release_record",
        lambda *_a, **_k: (True, {"needed": False, "attempted": False}),
    )
    monkeypatch.setattr(R, "_release_changed_paths", lambda _root: (set(), ""))
    monkeypatch.setattr(
        R, "compose_with_retries",
        lambda *_a, **_k: (0, "", [{
            "attempt": 1, "exit_code": 0, "category": None,
            "summary": None,
        }]),
    )


def _remote(ref: str, state=R.Presence.PRESENT,
            identity: str | None = "c" * 64) -> R.PresenceMeasurement:
    return R.PresenceMeasurement(
        state, ref, "stubbed-registry", state.value,
        identity if state is R.Presence.PRESENT else None,
    )


def _release_sh(*, latest_rc: int = 0):
    calls = []

    def run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if (cmd[:2] == ["docker", "push"]
                and cmd[-1] == f"{R.RELEASE_IMAGE}:latest"):
            if latest_rc:
                return latest_rc, "", "registry rejected latest"
        return 0, "ok", ""

    return calls, run


def test_remote_manifest_identity_is_canonical_not_json_order(
        monkeypatch):
    replies = deque([
        '{"schemaVersion":2,"config":{"digest":"sha256:abc"}}',
        '{"config":{"digest":"sha256:abc"},"schemaVersion":2}',
    ])
    monkeypatch.setattr(R, "_sh",
                        lambda *_a, **_k: (0, replies.popleft(), ""))

    version = R._probe_remote_manifest(f"{R.RELEASE_IMAGE}:0.2.30")
    latest = R._probe_remote_manifest(f"{R.RELEASE_IMAGE}:latest")

    assert version.state is R.Presence.PRESENT
    assert version.identity and version.identity == latest.identity


def test_remote_manifest_identity_rejects_duplicate_and_nonfinite_json(
        monkeypatch):
    replies = iter([
        '{"schemaVersion":2,"schemaVersion":1}',
        '{"schemaVersion":2,"size":NaN}',
    ])
    monkeypatch.setattr(R, "_sh", lambda *_a, **_k: (0, next(replies), ""))

    duplicate = R._probe_remote_manifest(f"{R.RELEASE_IMAGE}:0.2.30")
    nonfinite = R._probe_remote_manifest(f"{R.RELEASE_IMAGE}:latest")

    assert duplicate.state is R.Presence.PRESENT and not duplicate.identity
    assert nonfinite.state is R.Presence.PRESENT and not nonfinite.identity


def test_release_record_push_false_is_fatal_and_current_round_recorded(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    _calls, shell = _release_sh()
    monkeypatch.setattr(R, "_sh", shell)
    version_asks = {"count": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            version_asks["count"] += 1
            if version_asks["count"] == 1:
                return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "commit_release_record",
                        lambda *_a, **_k: (True, "record committed"))
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: R.PresenceMeasurement(
            R.Presence.ABSENT, "origin/main:RELEASED.json", "stubbed-git",
            "record commit is not on origin/main"),
    )
    pushed = []

    def refuse_push(*_a, **_k):
        pushed.append(True)
        return False, "NOT PUSHED (stubbed refusal)"

    monkeypatch.setattr(R, "push_release_record", refuse_push)
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["status"] == "failed"
    assert result["failure"]["stage"] == "release_record_push"
    assert result["failure"]["category"] == "release_record_push_failed"
    assert result["release_record_publication"]["repair"] == {
        "ok": False, "note": "NOT PUSHED (stubbed refusal)",
    }
    assert pushed == [True], "an explicit absence did not trigger the retry"
    assert not any(call[:2] == ["docker", "tag"] for call in _calls)
    assert not any(call[:2] == ["docker", "push"]
                   and call[-1].endswith(":latest") for call in _calls)


def test_latest_push_false_is_fatal_but_version_record_is_still_published(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    calls, shell = _release_sh(latest_rc=9)
    monkeypatch.setattr(R, "_sh", shell)
    version_asks = {"count": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            version_asks["count"] += 1
            if version_asks["count"] == 1:
                return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "commit_release_record",
                        lambda *_a, **_k: (True, "record committed"))
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: R.PresenceMeasurement(
            R.Presence.PRESENT, "origin/main:RELEASED.json", "stubbed-git",
            "record commit is published", "b" * 40),
    )
    monkeypatch.setattr(
        R, "push_release_record",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a present record must not be pushed again")),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["status"] == "failed"
    assert result["failure"]["stage"] == "latest_push"
    assert result["failure"]["category"] == "latest_push_failed"
    assert result["latest_publication"]["pushed"] is False
    assert result["release_record_publication"]["verified"] is True
    assert (root / "VERSION").read_text().strip() == "0.2.31"
    assert json.loads((root / "RELEASED.json").read_text())["version"] == "0.2.31"
    assert ["docker", "push", f"{R.RELEASE_IMAGE}:latest"] in calls


@pytest.mark.parametrize("readback", ["absent", "unknown", "no_identity"])
def test_zero_rc_immutable_push_must_read_back_before_any_later_writer(
        tmp_path, monkeypatch, readback):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    calls, shell = _release_sh()
    monkeypatch.setattr(R, "_sh", shell)
    asks = {"count": 0}

    def registry(ref):
        assert ref.endswith(":0.2.31")
        asks["count"] += 1
        if asks["count"] == 1:
            return _remote(ref, R.Presence.ABSENT)
        if readback == "absent":
            return _remote(ref, R.Presence.ABSENT)
        if readback == "unknown":
            return _remote(ref, R.Presence.UNKNOWN)
        return _remote(ref, identity=None)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    record_writes = []
    monkeypatch.setattr(
        R, "write_released_record",
        lambda *_a, **_k: record_writes.append(True) or {},
    )
    commits = []
    monkeypatch.setattr(
        R, "commit_release_record",
        lambda *_a, **_k: commits.append(True) or (True, "impossible"),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["pushed"] is True, "the irreversible push rc was lost"
    assert result["failure"]["stage"] == "immutable_publication"
    assert result["status"] == (
        "failed" if readback == "absent" else "unmeasured")
    assert record_writes == [] and commits == []
    assert (root / "VERSION").read_text(encoding="utf-8") == "0.2.30\n"
    assert not (root / "RELEASED.json").exists()
    assert not any(call[:2] == ["docker", "tag"] for call in calls)
    assert not any(call[:2] == ["docker", "push"]
                   and call[-1].endswith(":latest") for call in calls)


def test_record_push_zero_but_failed_readback_does_not_move_latest(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    calls, shell = _release_sh()
    monkeypatch.setattr(R, "_sh", shell)
    version_asks = {"count": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            version_asks["count"] += 1
            if version_asks["count"] == 1:
                return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "commit_release_record",
                        lambda *_a, **_k: (True, "record committed"))
    record_states = deque([R.Presence.ABSENT, R.Presence.ABSENT])

    def record_probe(*_a, **_k):
        state = record_states.popleft()
        return R.PresenceMeasurement(
            state, "origin/main:RELEASED.json", "stubbed-git", state.value,
        )

    monkeypatch.setattr(R, "_probe_release_record_publication", record_probe)
    monkeypatch.setattr(R, "push_release_record",
                        lambda *_a, **_k: (True, "pushed"))
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["failure"]["stage"] == "release_record_push"
    assert result["release_record_publication"]["final_state"] == "absent"
    assert not record_states
    assert not any(call[:2] == ["docker", "tag"] for call in calls)
    assert not any(call[:2] == ["docker", "push"]
                   and call[-1].endswith(":latest") for call in calls)


def test_latest_waits_for_immutable_and_record_authority_readbacks(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    events = []

    def shell(cmd, *_a, **_k):
        if cmd[:2] == ["docker", "push"] and cmd[-1].endswith(":0.2.31"):
            events.append("immutable-push")
        elif cmd[:2] == ["docker", "tag"]:
            events.append("latest-tag")
        elif cmd[:2] == ["docker", "push"] and cmd[-1].endswith(":latest"):
            events.append("latest-push")
        return 0, "ok", ""

    monkeypatch.setattr(R, "_sh", shell)
    version_asks = {"count": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            version_asks["count"] += 1
            if version_asks["count"] == 1:
                events.append("pre-push-absence")
                return _remote(ref, R.Presence.ABSENT)
            if version_asks["count"] == 2:
                events.append("immutable-readback")
            elif version_asks["count"] == 3:
                events.append("immutable-pre-latest")
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)

    def commit(*_a, **_k):
        events.append("record-commit")
        return True, "record committed"

    monkeypatch.setattr(R, "commit_release_record", commit)

    def record_probe(*_a, **_k):
        events.append("record-readback")
        return R.PresenceMeasurement(
            R.Presence.PRESENT, "origin/main:RELEASED.json", "stubbed-git",
            "published", "b" * 40,
        )

    monkeypatch.setattr(R, "_probe_release_record_publication", record_probe)
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])

    assert rc == R.RC_OK
    assert events.index("immutable-push") < events.index("immutable-readback")
    assert events.index("immutable-readback") < events.index("record-commit")
    assert events.index("record-commit") < events.index("record-readback")
    assert events.index("record-readback") < events.index("immutable-pre-latest")
    assert events.index("immutable-pre-latest") < events.index("latest-tag")
    assert events.index("latest-tag") < events.index("latest-push")


@pytest.mark.parametrize("hostile_dirty", ["none", "unrelated", "staged_pin"])
def test_next_noop_recovers_commit_failure_but_refuses_mixed_dirty_tree(
        tmp_path, monkeypatch, hostile_dirty):
    """A proven immutable release self-heals; unrelated work never rides it."""
    origin = tmp_path / "origin.git"
    root = tmp_path / "eda"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True)
    _git(root, "config", "user.name", "Release Test")
    _git(root, "config", "user.email", "release@example.invalid")
    _git(root, "remote", "add", "origin", str(origin))
    (root / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (root / "RELEASED.json").write_text(json.dumps({
        "version": "0.2.30",
        "pins_fingerprint": "previous",
        "pins": {"yosys": PIN},
    }) + "\n", encoding="utf-8")
    (root / "NOTES.txt").write_text("operator notes\n", encoding="utf-8")
    pin_file = root / "tools" / "yosys" / "Dockerfile"
    pin_file.parent.mkdir(parents=True)
    pin_file.write_text(
        "ARG YOSYS_REF=" + "a" * 40 + "\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "push", "-q", "-u", "origin", "main")
    base = _git(root, "rev-parse", "HEAD")

    real_reader = R.released_record
    real_recovery = R.recover_uncommitted_release_record
    real_commit = R.commit_release_record
    real_sh = R._sh
    real_changed_paths = R._release_changed_paths
    real_subprocess_run = subprocess.run
    _stub_release_inputs(monkeypatch, root, {})
    monkeypatch.setattr(R, "released_record", real_reader)
    monkeypatch.setattr(R, "recover_uncommitted_release_record", real_recovery)
    monkeypatch.setattr(R, "_release_changed_paths", real_changed_paths)

    def subprocess_router(args, *pargs, **kwargs):
        if (isinstance(args, (list, tuple)) and len(args) >= 2
                and str(args[1]).endswith((
                    "check_pins_agree.py",
                    "check_dockerfile_layer_budget.py",
                ))):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_subprocess_run(args, *pargs, **kwargs)

    monkeypatch.setattr(R.subprocess, "run", subprocess_router)
    registry_state = {"version": None, "latest": "e" * 64}
    version_ref = f"{R.RELEASE_IMAGE}:0.2.31"
    latest_ref = f"{R.RELEASE_IMAGE}:latest"

    def registry(ref):
        identity = (registry_state["version"] if ref == version_ref
                    else registry_state["latest"])
        if identity is None:
            return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity=identity)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)

    def shell(cmd, *args, **kwargs):
        if cmd and cmd[0] == "/usr/bin/env":
            return real_sh(cmd, *args, **kwargs)
        if cmd[:2] == ["docker", "push"] and cmd[-1] == version_ref:
            registry_state["version"] = "d" * 64
            return 0, "pushed", ""
        if cmd[:4] == ["docker", "buildx", "imagetools", "create"]:
            registry_state["latest"] = registry_state["version"]
            return 0, "copied", ""
        return 0, "ok", ""

    monkeypatch.setattr(R, "_sh", shell)
    monkeypatch.setattr(R, "_release_lock_path",
                        lambda: tmp_path / "release.lock")
    commit_calls = {"count": 0}

    def fail_once_then_commit(*args, **kwargs):
        commit_calls["count"] += 1
        if commit_calls["count"] == 1:
            return False, "stubbed transient commit failure"
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(R, "commit_release_record", fail_once_then_commit)
    sidecar = tmp_path / "release.json"

    first_rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    first = json.loads(sidecar.read_text(encoding="utf-8"))

    assert first_rc == R.RC_NEEDS_HUMAN
    assert first["failure"]["stage"] == "release_record_commit"
    assert first["immutable_publication"]["identity"] == "d" * 64
    assert registry_state["latest"] == "e" * 64
    assert _git(root, "rev-parse", "HEAD") == base
    if hostile_dirty == "unrelated":
        (root / "NOTES.txt").write_text(
            "operator notes\nDO NOT COMMIT THIS\n", encoding="utf-8")
    elif hostile_dirty == "staged_pin":
        pin_file.write_text(
            "ARG YOSYS_REF=" + "b" * 40 +
            "\nRUN echo DO-NOT-COMMIT-THIS\n", encoding="utf-8")
        _git(root, "add", "--", "tools/yosys/Dockerfile")

    second_rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    second = json.loads(sidecar.read_text(encoding="utf-8"))

    if hostile_dirty != "none":
        assert second_rc == R.RC_NEEDS_HUMAN
        assert second["failure"]["stage"] == "release_record_recovery"
        if hostile_dirty == "unrelated":
            assert second["failure"]["category"] == \
                "recovery_mixed_changes"
            assert second["release_record_recovery"]["mixed_paths"] == [
                "NOTES.txt"]
            assert "DO NOT COMMIT THIS" in (root / "NOTES.txt").read_text()
        else:
            assert second["failure"]["category"] == \
                "recovery_pin_changes_refused"
            assert second["release_record_recovery"][
                "refused_pin_files"]
            assert "DO-NOT-COMMIT-THIS" in pin_file.read_text()
        assert _git(root, "rev-parse", "HEAD") == base
        assert registry_state["latest"] == "e" * 64
        assert commit_calls["count"] == 1
    else:
        assert second_rc == R.RC_OK
        assert second["status"] == "noop"
        assert second["release_record_recovery"]["attempted"] is True
        assert second["release_record_recovery"]["verified"] is True
        assert registry_state["latest"] == "d" * 64
        assert commit_calls["count"] == 2
        assert _git(root, "status", "--porcelain") == ""
        remote_record = json.loads(_git(
            origin, "show", "main:RELEASED.json"))
        assert remote_record["version"] == "0.2.31"
        assert remote_record["manifest_identity"] == "d" * 64


@pytest.mark.parametrize(
    ("dirty_kind", "dirty_path"),
    [("tracked", "Dockerfile"), ("untracked", "NEW.release-input")],
)
def test_new_release_refuses_dirty_source_before_any_writer_under_foreign_git_env(
        tmp_path, monkeypatch, dirty_kind, dirty_path):
    """The release source admission is complete and cannot inspect another repo."""
    root = tmp_path / "eda"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True)
    _git(root, "config", "user.name", "Release Test")
    _git(root, "config", "user.email", "release@example.invalid")
    (root / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored-cache/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    foreign = tmp_path / "foreign"
    subprocess.run(["git", "init", "-q", "-b", "main", str(foreign)],
                   check=True)
    _git(foreign, "config", "user.name", "Foreign Test")
    _git(foreign, "config", "user.email", "foreign@example.invalid")
    (foreign / "foreign.txt").write_text("clean\n", encoding="utf-8")
    _git(foreign, "add", "-A")
    _git(foreign, "commit", "-qm", "foreign base")

    if dirty_kind == "tracked":
        (root / dirty_path).write_text(
            "FROM scratch\nRUN echo human-change\n", encoding="utf-8")
    else:
        (root / dirty_path).write_text("human input\n", encoding="utf-8")
    ignored = root / "ignored-cache" / "artifact"
    ignored.parent.mkdir()
    ignored.write_text("may remain ignored\n", encoding="utf-8")

    marker = tmp_path / "external-diff-ran"
    external_diff = tmp_path / "external-diff"
    external_diff.write_text(
        "#!/bin/sh\ntouch '" + str(marker) + "'\nexit 0\n",
        encoding="utf-8",
    )
    external_diff.chmod(0o755)
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    (hostile_home / ".gitconfig").write_text(
        "[diff]\n\texternal = " + str(external_diff) + "\n"
        "[core]\n\thooksPath = /definitely/not/release/hooks\n",
        encoding="utf-8",
    )

    real_changed_paths = R._release_changed_paths
    real_subprocess_run = subprocess.run
    _stub_release_inputs(monkeypatch, root, {})
    monkeypatch.setattr(R, "_release_changed_paths", real_changed_paths)

    def subprocess_router(args, *pargs, **kwargs):
        if (isinstance(args, (list, tuple)) and len(args) >= 2
                and str(args[1]).endswith((
                    "check_pins_agree.py",
                    "check_dockerfile_layer_budget.py",
                ))):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_subprocess_run(args, *pargs, **kwargs)

    monkeypatch.setattr(R.subprocess, "run", subprocess_router)
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "bogus-git-exec"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(external_diff))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE", "true")
    monkeypatch.setenv("GIT_DIFF_OPTS", "--stat")

    writer_calls = []

    def writer(name, answer):
        def call(*_args, **_kwargs):
            writer_calls.append(name)
            return answer
        return call

    monkeypatch.setattr(R, "write_recipe_vars", writer("recipe", []))
    monkeypatch.setattr(R, "rewrite_pin", writer("pin", []))
    monkeypatch.setattr(R, "retag_images", writer("retag", []))
    monkeypatch.setattr(R, "write_released_record",
                        writer("record", {}))
    monkeypatch.setattr(R, "commit_release_record",
                        writer("record-commit", (True, "committed")))
    monkeypatch.setattr(R, "_release_lock_path",
                        lambda: tmp_path / "release.lock")
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["failure"] == {
        "stage": "release_source_admission",
        "category": "release_source_dirty",
        "retryable": False,
        "summary": ("new release requires a clean source tree; dirty paths: "
                    + dirty_path),
    }
    assert result["source_admission"] == {
        "verified": False,
        "changed_paths": [dirty_path],
    }
    assert writer_calls == []
    assert marker.exists() is False
    assert ignored.is_file()


def test_published_terminal_proves_smoke_remote_names_record_and_anchor(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    _stub_release_inputs(monkeypatch, root, {})
    _calls, shell = _release_sh()
    monkeypatch.setattr(R, "_sh", shell)
    version_asks = {"count": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            version_asks["count"] += 1
            if version_asks["count"] == 1:
                return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "commit_release_record",
                        lambda *_a, **_k: (True, "record committed"))
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: R.PresenceMeasurement(
            R.Presence.PRESENT, "origin/main:RELEASED.json", "stubbed-git",
            "record commit is published", "b" * 40),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK and result["status"] == "published"
    assert result["exit_code"] == 0
    assert result["version"] == {"from": "0.2.30", "to": "0.2.31"}
    assert result["smoke"] == {
        "executed": True, "expected": len(R.SMOKE), "passed": len(R.SMOKE),
        "outcome": "natural", "failures": [],
    }
    assert result["registry_composed"] is True
    assert result["pushed"] is True
    assert result["latest_publication"]["pushed"] is True
    assert result["remote_publication"]["verified"] is True
    manifests = result["remote_publication"]["manifests"]
    assert manifests["version"]["identity"] == \
        manifests["latest"]["identity"] == "d" * 64
    assert result["release_record_publication"]["verified"] is True
    assert result["release_record_publication"]["identity"] == "b" * 40
    assert json.loads((root / "RELEASED.json").read_text(
        encoding="utf-8"))["manifest_identity"] == "d" * 64




def test_noop_fails_when_the_recorded_version_manifest_is_missing(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    asked = []

    def registry(ref):
        asked.append(ref)
        if ref.endswith(":0.2.30"):
            return _remote(ref, R.Presence.ABSENT)
        return _remote(ref)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a missing image must stop before record repair")),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["status"] == "failed"
    assert result["failure"]["category"] == \
        "recorded_version_manifest_missing"
    assert asked == [
        f"{R.RELEASE_IMAGE}:0.2.30", f"{R.RELEASE_IMAGE}:latest",
    ]


def test_noop_binds_remote_version_to_the_recorded_manifest_identity(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {
        "version": "0.2.30",
        "pins_fingerprint": _fingerprint(),
        "manifest_identity": "d" * 64,
    }
    _stub_release_inputs(monkeypatch, root, record)
    monkeypatch.setattr(
        R, "_probe_remote_manifest",
        lambda ref: _remote(ref, identity="e" * 64),
    )
    monkeypatch.setattr(
        R, "ensure_release_record_published",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("mismatched immutable identity authorised Git")),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["failure"]["category"] == \
        "recorded_manifest_identity_mismatch"
    assert "release_record_publication" not in result
    assert "latest_publication" not in result


def test_successful_noop_requires_matching_remote_manifests_and_record(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    monkeypatch.setattr(R, "_probe_remote_manifest", lambda ref: _remote(ref))
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: R.PresenceMeasurement(
            R.Presence.PRESENT, "origin/main:RELEASED.json", "stubbed-git",
            "record commit is published", "b" * 40),
    )
    monkeypatch.setattr(
        R, "push_release_record",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("verified publication must be an exact noop")),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK
    assert result["status"] == "noop"
    assert result["version"] == {"from": "0.2.30", "to": "0.2.30"}
    assert result["smoke"] == {
        "executed": False, "expected": len(R.SMOKE), "passed": 0,
        "outcome": "not_applicable", "failures": [],
    }
    assert result["remote_publication"]["verified"] is True
    assert result["remote_publication"]["manifests"]["version"]["identity"] \
        == result["remote_publication"]["manifests"]["latest"]["identity"]
    assert result["release_record_publication"]["verified"] is True
    assert result["release_record_publication"]["repair_attempted"] is False
    assert result["release_record_publication"]["identity"] == "b" * 40




def test_noop_repairs_stale_latest_from_immutable_remote_version(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    state = {"repaired": False}
    copies = []

    def registry(ref):
        identity = ("d" * 64 if ref.endswith(":0.2.30")
                    or state["repaired"] else "e" * 64)
        return _remote(ref, identity=identity)

    def repair(source, target):
        copies.append((source, target))
        state["repaired"] = True
        return 0, "copied", ""

    real_repair = R.repair_latest_publication
    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "_probe_release_record_publication",
                        lambda *_a, **_k: R.PresenceMeasurement(
                            R.Presence.PRESENT,
                            "origin/main:RELEASED.json", "stubbed-git",
                            "record commit is published", "record-commit"))
    monkeypatch.setattr(
        R, "repair_latest_publication",
        lambda version, repair=True, authorize=None,
        expected_identity=None: real_repair(
            version, probe=registry, publish=repair_fn,
            repair=repair, authorize=authorize,
            expected_identity=expected_identity,
        ),
    )
    repair_fn = repair
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK and result["status"] == "noop"
    assert copies == [(
        f"{R.RELEASE_IMAGE}:0.2.30", f"{R.RELEASE_IMAGE}:latest")]
    assert result["remote_publication"]["repair_attempted"] is True
    assert result["remote_publication"]["verified"] is True


def test_noop_does_not_mutate_when_remote_version_is_unmeasurable(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    calls = []

    def registry(ref):
        if ref.endswith(":0.2.30"):
            return _remote(ref, R.Presence.UNKNOWN, identity=None)
        return _remote(ref, identity="e" * 64)

    real_repair = R.repair_latest_publication
    monkeypatch.setattr(
        R, "repair_latest_publication",
        lambda version, repair=True, authorize=None,
        expected_identity=None: real_repair(
            version, probe=registry,
            publish=lambda *_a: calls.append(True),
            repair=repair, authorize=authorize,
            expected_identity=expected_identity,
        ),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["status"] == "unmeasured"
    assert result["failure"]["category"] == \
        "version_manifest_unmeasurable"
    assert calls == []


def test_noop_repairs_a_committed_record_missing_from_origin(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    monkeypatch.setattr(R, "_probe_remote_manifest", lambda ref: _remote(ref))
    states = deque([R.Presence.ABSENT, R.Presence.PRESENT])

    def record_probe(*_a, **_k):
        state = states.popleft()
        return R.PresenceMeasurement(
            state, "origin/main:RELEASED.json", "stubbed-git", state.value,
            "record-commit" if state is R.Presence.PRESENT else None,
        )

    monkeypatch.setattr(R, "_probe_release_record_publication", record_probe)
    pushes = []

    def repair(*_a, **_k):
        pushes.append(True)
        return True, "pushed"

    monkeypatch.setattr(R, "push_release_record", repair)
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK
    assert result["status"] == "noop"
    assert result["release_record_publication"]["repair_attempted"] is True
    assert result["release_record_publication"]["final_state"] == "present"
    assert pushes == [True]


def test_record_probe_uses_exact_fetched_commit_despite_stale_tracking_ref(
        tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.name", "Release Test")
    _git(seed, "config", "user.email", "release@example.invalid")
    (seed / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (seed / "RELEASED.json").write_text(
        '{"version":"0.2.30"}\n', encoding="utf-8")
    _git(seed, "add", "VERSION", "RELEASED.json")
    _git(seed, "commit", "-qm", "old release")
    old = _git(seed, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)

    # Advance the real remote, then remove the clone's fetch refspec. A plain
    # `git fetch origin main` now updates FETCH_HEAD only and leaves
    # refs/remotes/origin/main at `old`.
    (seed / "VERSION").write_text("0.2.31\n", encoding="utf-8")
    (seed / "RELEASED.json").write_text(
        '{"version":"0.2.31"}\n', encoding="utf-8")
    _git(seed, "add", "VERSION", "RELEASED.json")
    _git(seed, "commit", "-qm", "new release")
    new = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "main")
    _git(clone, "config", "--unset-all", "remote.origin.fetch")
    _git(clone, "fetch", "-q", "origin", "refs/heads/main")
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == old

    # A local replace ref makes ordinary `git show new:RELEASED.json` lie and
    # display the old record. The production probe must disable it.
    _git(clone, "replace", new, old)
    monkeypatch.setenv("GIT_DIR", str(seed / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(seed))
    monkeypatch.setenv("GIT_INDEX_FILE", str(seed / ".git" / "index"))
    measured = R._probe_release_record_publication(clone)

    assert measured.state is R.Presence.ABSENT
    assert "differs" in measured.detail
    refs = _git(clone, "for-each-ref", "--format=%(refname)",
                "refs/vibeic-release-probe")
    assert refs == "", "the private exact-fetch ref leaked"


def test_release_lock_refuses_concurrent_main_before_inner_program(
        tmp_path, monkeypatch):
    """A stale noop cannot overlap a newer release in another checkout."""
    lock = tmp_path / "locks" / "release.lock"
    sidecar = tmp_path / "release.json"
    monkeypatch.setattr(R, "_release_lock_path", lambda: lock)
    owner = R._acquire_release_lock(lock)
    calls = []
    monkeypatch.setattr(
        R, "_main_under_release_lock",
        lambda argv: calls.append(list(argv)) or R.RC_OK,
    )
    try:
        rc = R.main(["--json", str(sidecar)])
    finally:
        fcntl.flock(owner, fcntl.LOCK_UN)
        R.os.close(owner)

    result = json.loads(sidecar.read_text(encoding="utf-8"))
    assert rc == R.RC_NEEDS_HUMAN
    assert calls == []
    assert result["status"] == "blocked"
    assert result["failure"]["category"] == "release_already_running"

    # The refusal is not a sticky flag. Once the exact owner releases the
    # inode, a later round enters normally and releases its own descriptor.
    assert R.main(["--json", str(sidecar)]) == R.RC_OK
    assert calls == [["--json", str(sidecar)]]


def test_stale_noop_cannot_repair_latest_after_release_authority_advances():
    """Deterministic ordering from the independent concurrency review."""
    image = "ghcr.io/vibeic/vibeic-eda"
    old_identity = "a" * 64
    new_identity = "b" * 64
    state = {"latest": "c" * 64}
    writes = []

    def probe(ref):
        identity = old_identity if ref.endswith(":0.2.30") else state["latest"]
        return _remote(ref, identity=identity)

    def authority(_version):
        # A newer release wins after the stale noop's initial probes but before
        # its mutable-tag write. The second authority read must stop the old
        # invocation without allowing it to overwrite the newer latest.
        state["latest"] = new_identity
        return False, {
            "summary": "origin/main advanced to release 0.2.31",
            "state": "absent",
        }

    def publish(_source, _target):
        writes.append(True)
        state["latest"] = old_identity
        return 0, "", ""

    ok, evidence = R.repair_latest_publication(
        "0.2.30", image=image, probe=probe, publish=publish,
        authorize=authority,
    )

    assert ok is False
    assert evidence["category"] == "release_authority_changed"
    assert evidence["repair_attempted"] is False
    assert writes == []
    assert state["latest"] == new_identity


def test_latest_repair_refuses_replaced_version_after_git_authority():
    """The authorised name must still identify the initially frozen bytes."""
    old_identity = "a" * 64
    replacement = "b" * 64
    state = {"version": old_identity, "latest": "c" * 64}
    writes = []

    def probe(ref):
        identity = (state["version"] if ref.endswith(":0.2.30")
                    else state["latest"])
        return _remote(ref, identity=identity)

    def authority(_version):
        state["version"] = replacement
        return True, {"verified": True, "identity": "record-commit"}

    def publish(_source, _target):
        writes.append(True)
        state["latest"] = state["version"]
        return 0, "", ""

    ok, evidence = R.repair_latest_publication(
        "0.2.30", probe=probe, publish=publish, authorize=authority,
        expected_identity=old_identity,
    )

    assert ok is False
    assert evidence["category"] == "immutable_manifest_changed"
    assert evidence["frozen_identity"] == old_identity
    assert evidence["pre_repair_immutable"]["identity"] == replacement
    assert evidence["repair_attempted"] is False
    assert writes == []
    assert state["latest"] == "c" * 64


def test_latest_repair_final_identity_must_equal_frozen_record_identity():
    """Matching replacement version/latest tags cannot produce false green."""
    recorded = "a" * 64
    replacement = "b" * 64
    state = {"version": recorded, "latest": "c" * 64}

    def probe(ref):
        identity = (state["version"] if ref.endswith(":0.2.30")
                    else state["latest"])
        return _remote(ref, identity=identity)

    def publish(_source, _target):
        # The immutable name is replaced during the registry-side copy.  Both
        # final names agree, but on bytes neither the first probe nor Git
        # authorised.
        state.update(version=replacement, latest=replacement)
        return 0, "copied replacement", ""

    ok, evidence = R.repair_latest_publication(
        "0.2.30", probe=probe, publish=publish,
        authorize=lambda _version: (True, {"verified": True}),
        expected_identity=recorded,
    )

    assert ok is False
    assert evidence["category"] == "immutable_manifest_changed"
    assert evidence["repair_attempted"] is True
    assert evidence["final"]["verified"] is True
    assert evidence["final"]["manifests"]["version"]["identity"] == replacement
    assert evidence["verified"] is False


def test_shell_publishers_share_the_python_release_lock(tmp_path):
    helper = Path(R.__file__).resolve().parent / "release_lock.sh"
    shown = subprocess.run(
        ["bash", "-c", '. "$1"; vibeic_release_lock_path', "bash",
         str(helper)],
        text=True, capture_output=True,
    )
    assert shown.returncode == 0, shown.stderr
    assert Path(shown.stdout.strip()) == R._release_lock_path()

    # The shell path and Python path contend on the same kernel flock, not just
    # on equal-looking strings.
    lock = tmp_path / "shared.lock"
    owner = R._acquire_release_lock(lock)
    command = [
        "bash", "-c",
        '. "$1"; vibeic_acquire_release_lock "$2"',
        "bash", str(helper), str(lock),
    ]
    try:
        blocked = subprocess.run(command, text=True, capture_output=True)
    finally:
        fcntl.flock(owner, fcntl.LOCK_UN)
        R.os.close(owner)
    assert blocked.returncode != 0
    assert "another release owns" in blocked.stderr
    assert subprocess.run(command, text=True, capture_output=True).returncode == 0


def test_daily_release_is_the_only_final_image_publication_authority():
    repo = Path(R.__file__).resolve().parents[1]
    manual = (repo / "release.sh").read_text(encoding="utf-8")
    promote = (repo / "fork-gatekeeper" / "build_and_regress.sh").read_text(
        encoding="utf-8")
    release = Path(R.__file__).read_text(encoding="utf-8")

    # The manual helper has no reachable tag/push implementation at all.  Its
    # old transaction moved latest without a release record or readback.
    assert "[REFUSED]" in manual and "daily_release.py" in manual
    assert "docker push" not in manual and "docker tag" not in manual

    # Keep legacy candidate verification, but remove the second publication
    # implementation rather than guarding a large unreachable writer forever.
    legacy = promote[promote.index("promote_all()") :promote.index("CANDS=")]
    assert "legacy GK_MODE=promote is retired" in legacy
    assert "docker tag" not in legacy and "docker push" not in legacy
    assert "git push" not in legacy

    # The sole remaining publisher holds its authority over the complete inner
    # transaction, including readback and terminal sidecar creation.
    assert release.index("def main(") < release.index(
        "return _main_under_release_lock(args)")
    assert release.index("_acquire_release_lock(lock_path)") < release.index(
        "return _main_under_release_lock(args)")


def test_retired_manual_entry_cannot_touch_docker(tmp_path):
    repo = Path(R.__file__).resolve().parents[1]
    helper_dir = tmp_path / "bin"
    helper_dir.mkdir()
    marker = tmp_path / "docker-called"
    docker = helper_dir / "docker"
    docker.write_text(
        f"#!/bin/sh\nprintf called >{marker}\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{helper_dir}:{env.get('PATH', '')}"

    run = subprocess.run(
        [str(repo / "release.sh"), "9.9.9"], env=env,
        text=True, capture_output=True,
    )

    assert run.returncode == 2
    assert "REFUSED" in run.stderr and "daily_release.py" in run.stderr
    assert not marker.exists(), "retired release.sh invoked Docker"


def test_dry_run_record_release_does_not_write_the_record(tmp_path,
                                                          monkeypatch):
    root = _root(tmp_path)
    record = root / "RELEASED.json"
    monkeypatch.setattr(R, "_release_lock_path",
                        lambda: tmp_path / "release.lock")

    rc = R.main([
        "--eda-root", str(root), "--dry-run",
        "--record-release", "0.2.30",
    ])

    assert rc == R.RC_OK
    assert not record.exists(), "--dry-run wrote RELEASED.json"


@pytest.mark.parametrize("wire", [
    '{"version":"0.2.29","version":"0.2.30",'
    '"pins_fingerprint":"irrelevant"}',
    '{"version":"0.2.30","pins_fingerprint":NaN}',
])
def test_malformed_authoritative_record_is_unmeasured_before_any_writer(
        tmp_path, monkeypatch, wire):
    root = _root(tmp_path)
    (root / "RELEASED.json").write_text(wire, encoding="utf-8")
    real_reader = R.released_record
    _stub_release_inputs(monkeypatch, root, {})
    monkeypatch.setattr(R, "released_record", real_reader)
    monkeypatch.setattr(R, "_release_lock_path",
                        lambda: tmp_path / "release.lock")
    writes = []
    monkeypatch.setattr(R, "write_recipe_vars",
                        lambda *_a: writes.append(True) or [])
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert result["status"] == "unmeasured"
    assert result["failure"]["category"] == "release_record_malformed"
    assert writes == []


def test_release_git_ignores_ambient_repo_config_helpers_and_hooks(
        tmp_path, monkeypatch):
    target = tmp_path / "target"
    foreign = tmp_path / "foreign"
    for repo in (target, foreign):
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                       check=True)
        _git(repo, "config", "user.name", "Release Test")
        _git(repo, "config", "user.email", "release@example.invalid")
        (repo / "VERSION").write_text("0.2.30\n", encoding="utf-8")
        (repo / "RELEASED.json").write_text(
            '{"version":"0.2.30"}\n', encoding="utf-8")
        (repo / "tools" / "x").mkdir(parents=True)
        (repo / "tools" / "x" / "Dockerfile").write_text(
            "ARG X_REF=" + "a" * 40 + "\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

    before_foreign = _git(foreign, "rev-parse", "HEAD")
    (target / "VERSION").write_text("0.2.31\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.31"}\n', encoding="utf-8")
    (target / "tools" / "x" / "Dockerfile").write_text(
        "ARG X_REF=" + "b" * 40 + "\n", encoding="utf-8")

    marker = tmp_path / "hook-ran"
    hook = target / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf ran >{marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    malicious_home = tmp_path / "home"
    malicious_home.mkdir()
    (malicious_home / ".gitconfig").write_text(
        '[core]\n\thooksPath = .git/hooks\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(malicious_home))
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "fake-git-core"))

    ok, note = R.commit_release_record(
        target, "0.2.31", ["tools/x/Dockerfile"])

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_EXEC_PATH"):
        monkeypatch.delenv(name)
    assert ok, note
    assert _git(target, "show", "HEAD:VERSION") == "0.2.31"
    assert _git(target, "show", "HEAD:tools/x/Dockerfile").endswith("b" * 40)
    assert _git(foreign, "rev-parse", "HEAD") == before_foreign
    assert not marker.exists(), "ambient/local commit hook executed"


def test_release_fetch_ignores_home_url_rewrite_and_git_exec_path(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    fake = tmp_path / "fake.git"
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "init", "--bare", "-q", str(fake)], check=True)
    _git(repo, "remote", "add", "origin", "audit://canonical/repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[url "file://{fake}"]\n\tinsteadOf = audit://canonical/repo\n',
        encoding="utf-8",
    )
    marker = tmp_path / "helper-ran"
    helper = helper_dir / "git-remote-audit"
    helper.write_text(
        f"#!/bin/sh\nprintf ran >{marker}\nexit 91\n", encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_EXEC_PATH", str(helper_dir))

    commit, why = R._fetch_remote_branch_commit(repo, "main")

    assert commit is None and "fetch failed" in why
    assert not marker.exists(), "ambient git-remote helper executed"


@pytest.mark.parametrize("attack", ["external", "textconv"])
def test_pin_admission_ignores_forged_diff_renderers(
        tmp_path, monkeypatch, attack):
    """Admission must inspect committed bytes, not helper-rendered output."""
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)],
                   check=True)
    _git(target, "config", "user.name", "Release Test")
    _git(target, "config", "user.email", "release@example.invalid")
    (target / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.30"}\n', encoding="utf-8")
    pin = target / "tools" / "x" / "Dockerfile"
    pin.parent.mkdir(parents=True)
    pin.write_text("ARG X_REF=" + "a" * 40 + "\n", encoding="utf-8")

    attrs = target / ".gitattributes"
    if attack == "textconv":
        attrs.write_text("tools/x/Dockerfile diff=forged\n", encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")

    (target / "VERSION").write_text("0.2.31\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.31"}\n', encoding="utf-8")
    pin.write_text(
        "ARG X_REF=" + "b" * 40 + "\nRUN echo human-change\n",
        encoding="utf-8",
    )
    before = _git(target, "rev-parse", "HEAD")
    marker = tmp_path / f"{attack}-diff-ran"
    helper = tmp_path / f"forge-{attack}-diff"
    if attack == "external":
        helper.write_text(
            "#!/bin/sh\n"
            f"printf ran >{marker}\n"
            "printf '%s\\n' "
            "'--- a/tools/x/Dockerfile' '+++ b/tools/x/Dockerfile' "
            "'-ARG X_REF=" + "a" * 40 + "' "
            "'+ARG X_REF=" + "b" * 40 + "'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(helper))
        monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=0")
    else:
        # Returning only line one for each blob hides the human RUN line while
        # retaining a plausible paired pin replacement.
        helper.write_text(
            "#!/bin/sh\n"
            f"printf ran >{marker}\n"
            "sed -n '1p' \"$1\"\n",
            encoding="utf-8",
        )
        _git(target, "config", "diff.forged.textconv", str(helper))
    helper.chmod(0o755)

    ok, note = R.commit_release_record(
        target, "0.2.31", ["tools/x/Dockerfile"])

    assert ok is False
    assert "REFUSED" in note and "non-pin change" in note
    assert _git(target, "rev-parse", "HEAD") == before
    assert "RUN echo human-change" in pin.read_text(encoding="utf-8")
    assert not marker.exists(), f"{attack} diff renderer executed"


def test_raw_source_admission_detects_mm_and_preserves_staged_pin_edit(
        tmp_path):
    """Equal-and-opposite index/worktree state is dirty and never overwritten."""
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)],
                   check=True)
    _git(target, "config", "user.name", "Release Test")
    _git(target, "config", "user.email", "release@example.invalid")
    pin = target / "tools" / "x" / "Dockerfile"
    pin.parent.mkdir(parents=True)
    base = "ARG X_REF=" + "a" * 40 + "\n"
    staged = base + "RUN echo staged-human-edit\n"
    pin.write_text(base, encoding="utf-8")
    (target / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.30"}\n', encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")

    pin.write_text(staged, encoding="utf-8")
    _git(target, "add", "--", "tools/x/Dockerfile")
    pin.write_text(base, encoding="utf-8")
    assert _git(target, "status", "--porcelain", "--",
                "tools/x/Dockerfile").startswith("MM ")

    changed, why = R._release_changed_paths(target)
    assert why == "" and changed is not None
    assert "tools/x/Dockerfile" in changed

    # Model the release writer changing only the pin after a buggy admission.
    # The record commit must still notice that its raw replacement would erase
    # the independently staged human line.
    pin.write_text("ARG X_REF=" + "b" * 40 + "\n", encoding="utf-8")
    (target / "VERSION").write_text("0.2.31\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.31"}\n', encoding="utf-8")
    before = _git(target, "rev-parse", "HEAD")

    ok, note = R.commit_release_record(
        target, "0.2.31", ["tools/x/Dockerfile"])

    assert ok is False
    assert "different staged pin edit" in note
    assert _git(target, "rev-parse", "HEAD") == before
    assert "staged-human-edit" in _git(
        target, "show", ":tools/x/Dockerfile")
    assert "staged-human-edit" not in pin.read_text(encoding="utf-8")


def test_raw_admission_and_staging_never_execute_clean_filter(
        tmp_path, monkeypatch):
    """Attributes cannot hide build bytes or rewrite the committed blob."""
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)],
                   check=True)
    _git(target, "config", "user.name", "Release Test")
    _git(target, "config", "user.email", "release@example.invalid")
    pin = target / "tools" / "x" / "Dockerfile"
    pin.parent.mkdir(parents=True)
    old_pin = "a" * 40
    new_pin = "b" * 40
    forged_pin = "f" * 40
    pin.write_text(f"ARG X_REF={old_pin}\n", encoding="utf-8")
    (target / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.30"}\n', encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")

    marker = tmp_path / "clean-filter-ran"
    helper = tmp_path / "hostile-clean-filter"
    helper.write_text(
        "#!/bin/sh\n"
        f"touch '{marker}'\n"
        "sed -e '/HUMAN/d' "
        f"-e 's/^ARG X_REF={new_pin}$/ARG X_REF={forged_pin}/'\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(target, "config", "filter.hostile.clean", str(helper))
    (target / ".git" / "info" / "attributes").write_text(
        "tools/x/Dockerfile filter=hostile\n", encoding="utf-8")

    # Prove the fixture is hostile: ordinary Git diff runs the helper and the
    # filter makes an uncommitted build input disappear completely.
    pin.write_text(
        f"ARG X_REF={old_pin}\nRUN echo HUMAN-UNCOMMITTED\n",
        encoding="utf-8",
    )
    filtered = _git(target, "diff", "--", "tools/x/Dockerfile")
    assert filtered == "" and marker.exists()
    marker.unlink()

    changed, why = R._release_changed_paths(target)
    assert why == "" and changed is not None
    assert "tools/x/Dockerfile" in changed
    assert not marker.exists(), "raw source admission ran the clean filter"

    # On a valid release pin change, a later `git add` would execute the same
    # filter and replace b... with f.... Raw object plumbing must commit exactly
    # the bytes which the build consumed without invoking the helper at all.
    pin.write_text(f"ARG X_REF={new_pin}\n", encoding="utf-8")
    (target / "VERSION").write_text("0.2.31\n", encoding="utf-8")
    (target / "RELEASED.json").write_text(
        '{"version":"0.2.31"}\n', encoding="utf-8")
    monkeypatch.setattr(R, "push_release_record",
                        lambda *_a, **_k: (True, "stubbed push"))
    helpers = []
    real_sh = R._sh
    real_git_bytes = R._release_git_bytes

    def observed_sh(cmd, *args, **kwargs):
        answer = real_sh(cmd, *args, **kwargs)
        if marker.exists():
            helpers.append(cmd)
            marker.unlink()
        return answer

    def observed_git_bytes(root, *args, **kwargs):
        answer = real_git_bytes(root, *args, **kwargs)
        if marker.exists():
            helpers.append(list(args))
            marker.unlink()
        return answer

    monkeypatch.setattr(R, "_sh", observed_sh)
    monkeypatch.setattr(R, "_release_git_bytes", observed_git_bytes)

    ok, note = R.commit_release_record(
        target, "0.2.31", ["tools/x/Dockerfile"])

    assert ok, note
    assert helpers == [], f"release Git ran clean filter in: {helpers}"
    assert not marker.exists(), "raw record staging ran the clean filter"
    committed = _git(target, "show", "HEAD:tools/x/Dockerfile")
    assert committed == f"ARG X_REF={new_pin}"
    assert forged_pin not in committed


def test_noop_publishes_missing_record_before_repairing_stale_latest(
        tmp_path, monkeypatch):
    """The two incomplete publication halves converge in one locked round."""
    root = _root(tmp_path)
    record = {"version": "0.2.30", "pins_fingerprint": _fingerprint()}
    _stub_release_inputs(monkeypatch, root, record)
    state = {"latest": "e" * 64}
    immutable = "d" * 64
    events = []

    def registry(ref):
        identity = immutable if ref.endswith(":0.2.30") else state["latest"]
        return _remote(ref, identity=identity)

    record_states = deque([
        R.Presence.ABSENT, R.Presence.PRESENT, R.Presence.PRESENT,
    ])

    def record_probe(*_a, **_k):
        measured = record_states.popleft()
        events.append(("record-probe", measured.value))
        return R.PresenceMeasurement(
            measured, "origin/main:RELEASED.json", "stubbed-git",
            measured.value,
            "record-commit" if measured is R.Presence.PRESENT else None,
        )

    def push_record(*_a, **_k):
        events.append(("record-push", "0.2.30"))
        return True, "pushed"

    def push_latest(_source, _target):
        events.append(("latest-push", "0.2.30"))
        state["latest"] = immutable
        return 0, "copied", ""

    real_repair = R.repair_latest_publication
    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    monkeypatch.setattr(R, "_probe_release_record_publication", record_probe)
    monkeypatch.setattr(R, "push_release_record", push_record)
    monkeypatch.setattr(
        R, "repair_latest_publication",
        lambda version, repair=True, authorize=None,
        expected_identity=None: real_repair(
            version, probe=registry, publish=push_latest,
            repair=repair, authorize=authorize,
            expected_identity=expected_identity,
        ),
    )
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK
    assert result["status"] == "noop"
    assert result["release_record_publication"]["verified"] is True
    assert result["remote_publication"]["verified"] is True
    assert events.index(("record-push", "0.2.30")) < events.index(
        ("latest-push", "0.2.30"))
    assert state["latest"] == immutable
    assert not record_states


def test_dry_run_does_not_rewrite_real_recipe_digest(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "tools" / "yosys").mkdir(parents=True)
    (root / "tools" / "yosys" / "Dockerfile").write_text(
        f"ARG YOSYS_REF={PIN}\n", encoding="utf-8")
    bake = root / "docker-bake.hcl"
    bake.write_text(
        'variable "YOSYS_RECIPE" { default = "stale" }\n',
        encoding="utf-8",
    )
    before = bake.read_bytes()
    real_write = R.write_recipe_vars
    _stub_release_inputs(monkeypatch, root, {
        "version": "0.2.30", "pins_fingerprint": "unreleased",
    })
    monkeypatch.setattr(R, "write_recipe_vars", real_write)
    lock = tmp_path / "release.lock"
    monkeypatch.setattr(R, "_release_lock_path", lambda: lock)

    rc = R.main(["--eda-root", str(root), "--dry-run"])

    assert rc == R.RC_OK
    assert bake.read_bytes() == before


def test_recipe_only_release_seeds_first_bake_write_into_record_commit(
        tmp_path, monkeypatch):
    """The first recipe write cannot disappear when the second is idempotent."""
    root = _root(tmp_path)
    (root / "tools" / "yosys").mkdir(parents=True)
    (root / "tools" / "yosys" / "Dockerfile").write_text(
        f"ARG YOSYS_REF={PIN}\n", encoding="utf-8")
    bake = root / "docker-bake.hcl"
    bake.write_text(
        'variable "YOSYS_RECIPE" { default = "stale" }\n',
        encoding="utf-8",
    )
    real_write = R.write_recipe_vars
    _stub_release_inputs(monkeypatch, root, {
        "version": "0.2.30", "pins_fingerprint": "previous",
    })
    monkeypatch.setattr(R, "write_recipe_vars", real_write)
    _calls, shell = _release_sh()
    monkeypatch.setattr(R, "_sh", shell)
    asks = {"version": 0}

    def registry(ref):
        if ref.endswith(":0.2.31"):
            asks["version"] += 1
            if asks["version"] == 1:
                return _remote(ref, R.Presence.ABSENT)
        return _remote(ref, identity="d" * 64)

    monkeypatch.setattr(R, "_probe_remote_manifest", registry)
    committed_pin_files = []

    def commit(_root, _version, pin_files=None):
        committed_pin_files.extend(pin_files or [])
        return True, "record committed"

    monkeypatch.setattr(R, "commit_release_record", commit)
    monkeypatch.setattr(
        R, "_probe_release_record_publication",
        lambda *_a, **_k: R.PresenceMeasurement(
            R.Presence.PRESENT, "origin/main:RELEASED.json", "stubbed-git",
            "record commit is published", "b" * 40,
        ),
    )
    monkeypatch.setattr(R, "_release_lock_path",
                        lambda: tmp_path / "release.lock")
    sidecar = tmp_path / "release.json"

    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    result = json.loads(sidecar.read_text(encoding="utf-8"))

    assert rc == R.RC_OK and result["status"] == "published"
    assert 'default = "recipe-yosys"' in bake.read_text(encoding="utf-8")
    assert committed_pin_files == ["docker-bake.hcl"]


def test_recipe_only_record_reproduces_from_fresh_clone(tmp_path, monkeypatch):
    """The committed bake bytes recompute the exact recorded fingerprint."""
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)],
                   check=True)
    _git(target, "config", "user.name", "Release Test")
    _git(target, "config", "user.email", "release@example.invalid")
    tool = target / "tools" / "yosys" / "Dockerfile"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        "ARG YOSYS_REPO=https://github.com/vibeic/yosys.git\n"
        f"ARG YOSYS_REF={PIN}\n"
        "RUN git clone ${YOSYS_REPO} /y && git -C /y checkout ${YOSYS_REF}\n",
        encoding="utf-8",
    )
    (target / "Dockerfile").write_text(
        "ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:aaaaaaa\n"
        "FROM ${IMG_YOSYS}\n",
        encoding="utf-8",
    )
    bake = target / "docker-bake.hcl"
    bake.write_text(
        f'variable "YOSYS_REF" {{ default = "{PIN}" }}\n'
        'variable "YOSYS_RECIPE" { default = "stale" }\n'
        'target "yosys" {\n'
        '  context = "tools/yosys"\n'
        '  tags = tool_tags("yosys", YOSYS_REF)\n'
        '}\n',
        encoding="utf-8",
    )
    (target / "VERSION").write_text("0.2.30\n", encoding="utf-8")
    (target / "RELEASED.json").write_text("{}\n", encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")

    targets = {"yosys": ["YOSYS_REF"]}
    moved = R.write_recipe_vars(target, targets)
    assert moved == [f"YOSYS_RECIPE={R.recipe_hash(target, 'yosys')}"]
    version = "0.2.31"
    identity = "d" * 64
    (target / "VERSION").write_text(version + "\n", encoding="utf-8")
    written = R.write_released_record(
        target, version, targets, manifest_identity=identity)
    monkeypatch.setattr(R, "push_release_record",
                        lambda *_a, **_k: (True, "stubbed push"))

    ok, note = R.commit_release_record(
        target, version, ["docker-bake.hcl"])

    assert ok, note
    assert _git(target, "status", "--porcelain") == ""
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(target), str(fresh)],
                   check=True)
    recomputed = R.release_record_value(
        fresh, version, targets, manifest_identity=identity)
    assert R.released_record(fresh) == written == recomputed
    assert _git(fresh, "status", "--porcelain") == ""
