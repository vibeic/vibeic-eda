"""The release alarm printed "(no detail captured)" over a full explanation.

`check_release_failed` sourced its detail from the TOP LEVEL of the release
record only — `summary`, `reason`, `category`, `note`. A release that stops
records its reason under `failure` (a dict) and, for an unmeasurable outcome,
one entry per obstacle under `unmeasured`. None of those are top-level keys.

Observed on the 2026-08-30 round, which opened PR #148 asking a human to go and
find what the record already held:

    failure.category  = registry_artefact_state_unmeasurable
    failure.subject   = ghcr.io/vibeic/eda-tool-xyce:1fab458-71dfca
    failure.summary   = registry presence unmeasured: … TLS handshake timeout
    failure.retryable = true

i.e. a transient GHCR read. `retryable` IS the triage decision — retry it, or
send someone to look — so the alarm surfacing neither was the whole cost.
"""
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "crf_under_test", _HERE / "check_release_failed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_REAL_SHAPE = {
    "program": "daily_release",
    "status": "unmeasured",
    "exit_code": 1,
    "failure": {
        "stage": "artefact_discovery",
        "category": "registry_artefact_state_unmeasurable",
        "retryable": True,
        "subject": "ghcr.io/vibeic/eda-tool-xyce:1fab458-71dfca",
        "summary": "registry presence unmeasured: net/http: TLS handshake timeout",
    },
    "unmeasured": [{
        "stage": "artefact_discovery",
        "category": "registry_artefact_state_unmeasurable",
        "subject": "ghcr.io/vibeic/eda-tool-xyce:1fab458-71dfca",
        "detail": "registry presence unmeasured: net/http: TLS handshake timeout",
    }],
}


def test_the_nested_failure_is_reported():
    """THE REGRESSION: this exact record produced '(no detail captured)'."""
    d = _mod()._detail_from(_REAL_SHAPE)
    assert "registry_artefact_state_unmeasurable" in d, d
    assert "TLS handshake timeout" in d, d
    assert "eda-tool-xyce" in d, d


def test_retryable_is_surfaced_because_it_is_the_triage_decision():
    d = _mod()._detail_from(_REAL_SHAPE)
    assert "retryable" in d, d


def test_every_unmeasured_obstacle_is_listed():
    rec = dict(_REAL_SHAPE)
    rec["unmeasured"] = [
        {"category": "first_obstacle", "subject": "a"},
        {"category": "second_obstacle", "subject": "b"},
    ]
    d = _mod()._detail_from(rec)
    assert "first_obstacle" in d and "second_obstacle" in d, d


def test_a_record_with_nothing_to_say_stays_empty():
    """The other tail: the alarm must not manufacture detail. A clean record
    still falls through to '(no detail captured)', which is then TRUE."""
    assert _mod()._detail_from(
        {"program": "daily_release", "status": "published"}) == ""


def test_a_top_level_reason_still_reported():
    """The old sources must keep working."""
    d = _mod()._detail_from({"reason": "disk full"})
    assert "disk full" in d, d
