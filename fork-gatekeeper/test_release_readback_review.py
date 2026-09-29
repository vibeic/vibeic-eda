"""Adversarial obligations from the review of PR 270; no registry writes."""
import json

import pytest

import check_publish_silence as S
import check_release_failed as C
import daily_release as R
from test_a_logged_rc_is_the_commands_rc import scan, _scripts, ROOT


@pytest.mark.parametrize("first", [
    "unsupported manifest format: &{manifest}",
    "unsupported manifest format: &{manifest} TLS handshake timeout",
    "unsupported manifest format: &{manifest unknown}",
])
def test_existence_evidence_cannot_become_permission_to_overwrite(monkeypatch, first):
    replies = iter([(1, "", first), (1, "", "manifest unknown")])
    monkeypatch.setattr(R, "_sh", lambda *a, **k: next(replies))
    measured = R._probe_remote_manifest("registry/tool:immutable", sleep=lambda s: None)
    assert measured.state is R.Presence.UNKNOWN
    with pytest.raises(R.ReleaseMeasurementUnknown):
        R._require_measured(measured, stage="pre_push_registry_guard",
                            category="registry_unknown")


def test_network_only_retry_window_stays_short(monkeypatch):
    calls, delays = [], []
    def sh(*a, **k):
        calls.append(a)
        return 1, "", "TLS handshake timeout"
    monkeypatch.setattr(R, "_sh", sh)
    assert R._probe_remote_manifest("r:t", sleep=delays.append).state is R.Presence.UNKNOWN
    assert len(calls) == 3 and delays == [2, 10]


def test_alert_delivery_refusal_has_its_own_rc(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_default_escalate", lambda *a, **k: (False, "refused"))
    assert C.main(["--tick-exit", "5", "--round-at", "2026-09-30",
                   "--release-json", str(tmp_path / "missing")]) == 3


def test_multiline_values_cannot_crowd_out_triage_keys():
    text = "long line\n" * 10000
    record = {"summary": text, "failure": {
        "summary": text, "detail": text, "stage": "publication",
        "category": "unknown", "subject": "r:t", "retryable": False}}
    detail = C._detail_from(record)
    _, body = C._report(5, "ran", "2026-09-30", detail)
    for key, value in [("stage", "publication"), ("category", "unknown"),
                       ("subject", "r:t"), ("retryable", "False")]:
        assert f"failure.{key}: {value}" in body.splitlines()
    assert max(map(len, detail.splitlines())) <= C.DETAIL_LINE_CAP
    assert len(body) <= C.BODY_CAP


@pytest.mark.parametrize("snippet", [
    'echo "$(true) rc=${?}"',
    'echo $(true) 2>&1 rc=$?',
    'echo $(true) 2>&1 rc=${?}',
])
def test_status_scanner_covers_braces_and_redirection(snippet):
    assert scan(snippet)[0] == [1]


def test_unterminated_ansi_quote_is_not_certified():
    assert scan("echo $'text\\'more")[1]


def test_script_inventory_includes_root_and_shebang_files(tmp_path, monkeypatch):
    import test_a_logged_rc_is_the_commands_rc as L
    (tmp_path / "release.sh").write_text("#!/bin/bash\ntrue\n")
    (tmp_path / "runner").write_text("#!/usr/bin/env bash\ntrue\n")
    (tmp_path / "python-runner").write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(L, "ROOT", tmp_path)
    assert {p.name for p in L._scripts()} == {"release.sh", "runner"}


@pytest.mark.parametrize("lookup", [(1, "lookup failed"), (0, "invalid json")])
def test_unknown_alert_lookup_does_not_claim_delivery(monkeypatch, tmp_path, lookup):
    monkeypatch.setattr(S, "REPO", tmp_path)
    calls = []
    def sh(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("gh", "pr", "list"):
            return lookup
        return 0, ""
    monkeypatch.setattr(S, "_sh", sh)
    ok, detail = S._default_escalate("title", "body", "2026-09-30")
    assert not ok and "lookup" in detail
    assert not any("push" in a or "create" in a for a in calls)


def test_orphan_alert_branch_is_retried_without_force(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "REPO", tmp_path)
    calls = []
    def sh(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("gh", "pr", "list"):
            return 0, "[]"
        if "ls-remote" in args:
            return 0, "abc123\trefs/heads/publish-silence-2026-09-30\n"
        if "create" in args:
            return 0, "https://github.com/vibeic/vibeic-eda/pull/999"
        return 0, ""
    monkeypatch.setattr(S, "_sh", sh)
    ok, detail = S._default_escalate("title", "body", "2026-09-30")
    assert ok and "opened alert PR" in detail
    assert any("create" in a for a in calls)
    assert not any("--force" in a and "push" in a for a in calls)
