"""Regression gates for the pre-publish Docker layer-depth refusal."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_image_depth", ROOT / "tools" / "check_image_depth.py"
)
assert SPEC and SPEC.loader
DEPTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPTH)


def _docker(monkeypatch, *, count="125", rc=0, stderr=""):
    monkeypatch.setattr(
        DEPTH.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=rc, stdout=f"{count}\n", stderr=stderr
        ),
    )


def test_depth_at_limit_is_pullable(monkeypatch, capsys):
    _docker(monkeypatch, count="125")
    assert DEPTH.main(["candidate:tag"]) == 0
    assert "125/125 pullable layers" in capsys.readouterr().out


def test_depth_over_limit_is_refused(monkeypatch, capsys):
    _docker(monkeypatch, count="126")
    assert DEPTH.main(["candidate:tag"]) == 1
    err = capsys.readouterr().err
    assert "126 layers" in err
    assert "Compact the Dockerfile and rebuild" in err


def test_depth_over_limit_is_compacted_when_release_requests_it(monkeypatch, capsys):
    _docker(monkeypatch, count="126")
    calls = []

    def compact(image, limit, **kwargs):
        calls.append((image, limit, kwargs))
        return 2

    monkeypatch.setattr(DEPTH, "compact_image", compact)
    assert DEPTH.main(["--compact", "candidate:tag"]) == 0
    assert calls == [("candidate:tag", 125, {
        "stall_grace_s": DEPTH.DEFAULT_COMPACTION_STALL_GRACE_S,
    })]
    assert "126 -> 2/125 pullable layers" in capsys.readouterr().out


def test_failed_compaction_is_a_refusal_and_keeps_the_reason(monkeypatch, capsys):
    _docker(monkeypatch, count="126")

    def compact(_image, _limit, **_kwargs):
        raise DEPTH.CompactionError("metadata changed")

    monkeypatch.setattr(DEPTH, "compact_image", compact)
    assert DEPTH.main(["--compact", "candidate:tag"]) == 2
    err = capsys.readouterr().err
    assert "automatic compaction failed" in err
    assert "metadata changed" in err


def test_stalled_compaction_is_not_misreported_as_unpublishable(
        monkeypatch, capsys):
    _docker(monkeypatch, count="126")

    def compact(_image, _limit, **_kwargs):
        raise DEPTH.CompactionStalled("byte counter stayed flat")

    monkeypatch.setattr(DEPTH, "compact_image", compact)
    assert DEPTH.main(["--compact", "candidate:tag"]) == DEPTH.watchdog.RC_STALLED
    err = capsys.readouterr().err
    assert "[STALLED]" in err and "byte counter" in err
    assert "unpublishable" not in err


def test_unmeasurable_depth_is_not_a_pass(monkeypatch, capsys):
    _docker(monkeypatch, rc=1, stderr="daemon unavailable")
    assert DEPTH.main(["candidate:tag"]) == 2
    assert "cannot prove image depth" in capsys.readouterr().err


def test_compaction_rejects_config_it_cannot_preserve_losslessly():
    for key, value in (
        ("Volumes", {"/data": {}}),
        ("Healthcheck", {"Test": ["CMD", "true"]}),
        ("StopSignal", "SIGQUIT"),
        ("Shell", ["/bin/bash", "-c"]),
    ):
        try:
            DEPTH._config_changes({key: value})
        except DEPTH.CompactionError as exc:
            assert key in str(exc)
        else:
            raise AssertionError(f"{key} was silently discarded")


def test_compaction_restores_the_release_images_runtime_contract():
    config = {
        "User": "1000",
        "Env": ["PATH=/bin", "WITH_SPACE=one two"],
        "Entrypoint": ["/entrypoint"],
        "Cmd": ["--wait"],
        "WorkingDir": "/foss/designs",
        "ExposedPorts": {"80/tcp": {}, "5901/tcp": {}},
        "Labels": {"org.opencontainers.image.title": "vibeic eda"},
    }
    changes = DEPTH._config_changes(config)
    assert "USER 1000" in changes
    assert 'ENV WITH_SPACE="one two"' in changes
    assert 'ENTRYPOINT ["/entrypoint"]' in changes
    assert 'CMD ["--wait"]' in changes
    assert 'WORKDIR "/foss/designs"' in changes
    assert "EXPOSE 80/tcp" in changes
    assert 'LABEL "org.opencontainers.image.title"="vibeic eda"' in changes


def test_export_import_can_run_past_grace_while_bytes_advance(monkeypatch):
    """A large stream is bounded by progress, never by its total duration."""
    real = subprocess.Popen

    def popen(cmd, **kwargs):
        if cmd[:3] == ["docker", "container", "export"]:
            program = (
                "import os,time\n"
                "for i in range(8):\n"
                " os.write(1, b'x' * 4096); time.sleep(.04)\n"
            )
        else:
            program = "import sys; sys.stdin.buffer.read(); print('sha256:ok')\n"
        return real([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(DEPTH.subprocess, "Popen", popen)
    DEPTH._export_import(
        "container", "target", "linux/amd64",
        stall_grace_s=0.11, poll_s=0.02,
    )


def test_export_import_stalls_only_when_transferred_bytes_are_flat(monkeypatch):
    real = subprocess.Popen

    def popen(cmd, **kwargs):
        if cmd[:3] == ["docker", "container", "export"]:
            program = "import time; time.sleep(30)"
        else:
            program = "import sys; sys.stdin.buffer.read()"
        return real([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(DEPTH.subprocess, "Popen", popen)
    with pytest.raises(DEPTH.CompactionError, match="stalled.*transferred"):
        DEPTH._export_import(
            "container", "target", "linux/amd64",
            stall_grace_s=0.12, poll_s=0.03,
        )


def test_every_release_path_checks_depth_before_it_pushes():
    manual = (ROOT / "release.sh").read_text()
    daily = (ROOT / "fork-gatekeeper" / "daily_release.py").read_text()
    legacy = (ROOT / "fork-gatekeeper" / "build_and_regress.sh").read_text()
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "tools/check_image_depth.py" in manual
    assert 'check_image_depth.py" --compact' in manual
    assert manual.index("tools/check_image_depth.py") < manual.index('docker push "${t}"')

    compose_tail = daily[daily.index('result["registry_composed"] = True') :]
    assert "tools/check_image_depth.py" in compose_tail
    assert '"--compact", tag' in compose_tail
    assert "progress=True" in compose_tail
    assert "stall_grace=COMPACTION_STALL_GRACE_S" in compose_tail
    assert compose_tail.index("tools/check_image_depth.py") < compose_tail.index(
        '["docker", "push", tag]'
    )

    promote = legacy[legacy.index("promote_all()") :]
    assert "tools/check_image_depth.py" in promote
    assert "--compact" in promote
    assert promote.index("tools/check_image_depth.py") < promote.index(
        '_progress_exec docker push "ghcr.io/vibeic/vibeic-eda:${NEWVER}"'
    )

    assert "--load" in workflow
    assert "push: true" not in workflow[workflow.index("build-and-push:") :]
    assert "check_image_depth.py --compact" in workflow
    assert workflow.index("tools/check_image_depth.py") < workflow.index(
        "SRC_IMAGE=ghcr.io/vibeic/"
    )
