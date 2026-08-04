"""A failed upstream fetch must not be reported as 'already current'.

Measured 2026-08-04: `step1_upstream` discarded the fetch's exit status, so a clone
whose fetch failed compared against its last successfully-fetched ref and reported
behind == 0. One fork stayed 12 commits behind for a day while the round logged
"already current" every morning.

The control runs BOTH directions on purpose. Asserting only that a failed fetch is
reported would pass against a version that reports failure for every fetch; the
second test pins the other side.
"""
import subprocess, sys, types, pathlib
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D


def _fake_sh(rc, err=""):
    def f(*a, cwd=None):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, rc, "", err)
        return subprocess.CompletedProcess(a, 0, "", "")
    return f


def test_failed_fetch_is_reported_as_unknown_not_current(monkeypatch):
    monkeypatch.setattr(D, "sh", _fake_sh(128, "fatal: unable to access upstream"))
    monkeypatch.setattr(D, "out", lambda *a, **k: "https://example/upstream"
                        if "get-url" in a else "0")
    rep = {}
    D.step1_upstream(["git", "-C", "/nonexistent"], "master", rep)
    assert "FETCH FAILED" in rep["upstream"], rep
    assert "already current" not in rep["upstream"], rep


def test_successful_fetch_with_no_new_commits_is_still_current(monkeypatch):
    """The other direction: a working fetch that finds nothing must NOT be an error."""
    monkeypatch.setattr(D, "sh", _fake_sh(0))
    def fake_out(*a, **k):
        if "get-url" in a: return "https://example/upstream"
        if "rev-parse" in a: return "deadbeef"
        if "rev-list" in a: return "0"
        return ""
    monkeypatch.setattr(D, "out", fake_out)
    rep = {}
    D.step1_upstream(["git", "-C", "/nonexistent"], "master", rep)
    assert rep["upstream"] == "already current", rep
