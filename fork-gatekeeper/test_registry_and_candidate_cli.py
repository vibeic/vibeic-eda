from __future__ import annotations

import json
from pathlib import Path

import registry_presence as P
import release_candidate_smoke as S


def _measurement(state):
    return type("Measurement", (), {
        "state": state,
        "subject": "ghcr.io/vibeic/x:1",
        "source": "registry",
        "detail": "fixture",
    })()


def test_registry_presence_has_three_non_overlapping_answers(monkeypatch,
                                                             tmp_path):
    path = tmp_path / "presence.json"
    for state, rc in ((P.Presence.PRESENT, P.RC_PRESENT),
                      (P.Presence.ABSENT, P.RC_ABSENT),
                      (P.Presence.UNKNOWN, P.RC_UNKNOWN)):
        monkeypatch.setattr(P, "_probe_remote_manifest",
                            lambda _ref, state=state: _measurement(state))
        assert P.main(["ghcr.io/vibeic/x:1", "--json", str(path)]) == rc
        assert json.loads(path.read_text())["state"] == state.value


def test_candidate_smoke_norecord_is_not_a_tool_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "SMOKE", {"yosys": "yosys -V"})
    monkeypatch.setattr(S, "smoke_image", lambda _image: [{
        "tool": "yosys", "outcome": "stalled", "rc": 199,
        "err": "no semantic completion",
    }])
    path = tmp_path / "smoke.json"
    assert S.main(["candidate:1", "--json", str(path)]) == S.RC_NORECORD
    assert json.loads(path.read_text())["status"] == "norecord"


def test_candidate_smoke_only_judges_natural_completion(monkeypatch):
    monkeypatch.setattr(S, "SMOKE", {"yosys": "yosys -V"})
    monkeypatch.setattr(S, "smoke_image", lambda _image: [{
        "tool": "yosys", "outcome": "natural_failure", "rc": 9,
        "err": "bad binary",
    }])
    assert S.main(["candidate:1"]) == S.RC_FAILED
    monkeypatch.setattr(S, "smoke_image", lambda _image: [])
    assert S.main(["candidate:1"]) == S.RC_OK


def test_release_workflow_uses_three_state_registry_and_progress_leases():
    workflow = (Path(__file__).resolve().parents[1] /
                ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "timeout-minutes:" not in workflow
    assert "docker manifest inspect" not in workflow
    assert workflow.count("fork-gatekeeper/registry_presence.py") >= 2
    assert "fork-gatekeeper/release_candidate_smoke.py" in workflow
    assert workflow.count("fork-gatekeeper/progress_exec.py") >= 3
