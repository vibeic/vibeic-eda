"""Regression gates for the pre-publish Docker layer-depth refusal."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


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


def test_unmeasurable_depth_is_not_a_pass(monkeypatch, capsys):
    _docker(monkeypatch, rc=1, stderr="daemon unavailable")
    assert DEPTH.main(["candidate:tag"]) == 2
    assert "cannot prove image depth" in capsys.readouterr().err


def test_every_release_path_checks_depth_before_it_pushes():
    manual = (ROOT / "release.sh").read_text()
    daily = (ROOT / "fork-gatekeeper" / "daily_release.py").read_text()
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "tools/check_image_depth.py" in manual
    assert manual.index("tools/check_image_depth.py") < manual.index('docker push "${t}"')

    compose_tail = daily[daily.index('result["registry_composed"] = True') :]
    assert "tools/check_image_depth.py" in compose_tail
    assert compose_tail.index("tools/check_image_depth.py") < compose_tail.index(
        '["docker", "push", tag]'
    )

    assert "load: true" in workflow
    assert "push: true" not in workflow[workflow.index("build-and-push:") :]
    assert workflow.index("tools/check_image_depth.py") < workflow.index(
        "SRC_IMAGE=ghcr.io/vibeic/"
    )
