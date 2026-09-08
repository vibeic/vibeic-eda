"""A TRANSPORT failure on the live-HEAD probe is re-asked; a bad ANSWER is not.

MEASURED, 2026-09-08. The 05:30 round reported

    yosys  UPSTREAM_DEFAULT_UNMEASURABLE  live upstream HEAD did not resolve to
    exactly one branch and one full commit id (rc=127: TimeoutExpired: Command
    '[... 'ls-remote', '--symref', 'upstream', 'HEAD']' timed out after 120 seconds)

One unmeasured fork makes daily_merge NEEDS HUMAN, which makes the blocking gate
REFUSE daily_release, so one network blip cost the whole day's release. The same
command retried by hand four times minutes later answered in 0.7 s every time.

Refusing on an unmeasured fork is CORRECT and is not what these tests change.
They pin the distinction the retry rests on: a failure to REACH the remote says
nothing about its answer and may be re-asked; an answer that is ambiguous or
malformed is a finding ABOUT THE REMOTE, and re-asking it would be asking a
different question until it gives a nicer one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_merge as M  # noqa: E402


GOOD = "ref: refs/heads/main\tHEAD\n" + "7" * 40 + "\tHEAD\n"
TIMEOUT_ERR = ("TimeoutExpired: Command '['git', '-C', '/x', 'ls-remote', "
               "'--symref', 'upstream', 'HEAD']' timed out after 120 seconds")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _s: None)


def _probe(monkeypatch, answers):
    """Drive the real helper; `answers` is one (rc, out, err) per attempt."""
    calls = []

    def fake_sh(args, cwd=None, timeout=None, **kw):
        calls.append((list(args), timeout))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(M, "_sh", fake_sh)
    return M._live_head_with_transport_retry(Path("/x")), calls


def test_a_timeout_is_retried_and_the_second_answer_stands(monkeypatch):
    """THE REGRESSION: exactly what happened to yosys, and what should have."""
    (rc, out, err), calls = _probe(
        monkeypatch, [(127, "", TIMEOUT_ERR), (0, GOOD, "")])
    assert rc == 0 and "refs/heads/main" in out
    assert len(calls) == 2, calls
    # the retry must ask the SAME question with the SAME deadline
    assert calls[0][0] == calls[1][0]
    assert calls[0][1] == calls[1][1] == M.LIVE_HEAD_TIMEOUT_S
    assert calls[0][0][-4:] == ["ls-remote", "--symref", "upstream", "HEAD"]


@pytest.mark.parametrize("err", [
    TIMEOUT_ERR,
    "fatal: unable to access 'https://github.com/x/y.git/': Connection timed out",
    "ssh: connect to host github.com port 22: Connection refused",
    "fatal: Could not resolve host: github.com",
    "fatal: the remote end hung up unexpectedly",
    "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly",
])
def test_every_transport_shape_is_retried(monkeypatch, err):
    (rc, _out, _err), calls = _probe(monkeypatch, [(128, "", err), (0, GOOD, "")])
    assert rc == 0 and len(calls) == 2, (err, calls)


def test_a_persistent_outage_still_refuses_and_shows_every_attempt(monkeypatch):
    """A retry must not become a way to sit on an outage, and the diagnostic
    must carry all of the failures rather than only the last."""
    (rc, _out, err), calls = _probe(monkeypatch, [(127, "", TIMEOUT_ERR)])
    assert rc != 0
    assert len(calls) == M.LIVE_HEAD_ATTEMPTS == 3
    for i in (1, 2, 3):
        assert f"attempt {i}/3" in err, err


def test_an_ambiguous_answer_is_never_retried(monkeypatch):
    """THE BIDIRECTIONAL CONTROL. rc=0 means git spoke to the remote; two
    symrefs is a finding ABOUT THE REMOTE. Re-asking would be shopping for a
    nicer answer, and the caller must still report UNMEASURABLE."""
    two = "ref: refs/heads/main\tHEAD\nref: refs/heads/dev\tHEAD\n" + "7" * 40 + "\tHEAD\n"
    (rc, out, _err), calls = _probe(monkeypatch, [(0, two, "")])
    assert rc == 0 and out == two
    assert len(calls) == 1, "an answered-but-ambiguous probe was retried"


def test_a_non_transport_failure_is_not_retried(monkeypatch):
    """A repository that does not exist answers the same way every time; the
    retry budget is for the network, not for wrong configuration."""
    (rc, _o, _e), calls = _probe(
        monkeypatch, [(128, "", "remote: Repository not found.")])
    assert rc == 128
    assert len(calls) == 1, calls


def test_the_classifier_never_calls_a_successful_probe_a_transport_failure():
    assert M._is_transport_failure(0, GOOD, "") is False
    assert M._is_transport_failure(0, "", TIMEOUT_ERR) is False, (
        "rc=0 means the remote answered; its output must not be re-read as a "
        "transport failure")
    assert M._is_transport_failure(127, "", TIMEOUT_ERR) is True
    assert M._is_transport_failure(M.watchdog.RC_STALLED, "", "") is True


def test_the_caller_still_reports_unmeasurable_when_the_retries_fail(monkeypatch):
    """The gate this protects must be unchanged: three transport failures is
    still an unmeasured fork, still NEEDS HUMAN, still refuses the release."""
    src = Path("/x")
    monkeypatch.setattr(
        M, "_live_head_with_transport_retry",
        lambda _s: (127, "", "attempt 1/3 rc=127: " + TIMEOUT_ERR))
    rc, out, err = M._live_head_with_transport_retry(src)
    assert rc != 0 and "attempt 1/3" in err


# --------------------------------------------------------------------------
# The same rule at EVERY live probe, not only the one that failed today
# --------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    "error connecting to api.github.com",
    "Post \"https://api.github.com/graphql\": context deadline exceeded",
    "HTTP 502: Bad gateway",
    "HTTP 503: Service unavailable",
    "HTTP 429: API rate limit exceeded",
    "dial tcp: i/o timeout",
    "net/http: TLS handshake timeout",
    "dial tcp: lookup api.github.com: no such host",
])
def test_gh_transport_shapes_are_retried(monkeypatch, err):
    """The three `gh api` probes decide whether a fork was measured at all, so
    a 5xx / rate limit / dial failure has the same cost as today's git timeout:
    one unmeasured fork refuses the whole release."""
    (rc, _o, _e), calls = _probe(monkeypatch, [(1, "", err), (0, GOOD, "")])
    assert rc == 0 and len(calls) == 2, (err, calls)


@pytest.mark.parametrize("err", [
    "HTTP 404: Not Found",
    "HTTP 403: Resource not accessible by integration",
    "remote: Repository not found.",
])
def test_an_http_answer_is_not_retried(monkeypatch, err):
    """THE CONTROL for the widened classifier: a 4xx IS an answer (it does not
    exist / you may not see it) and repeats identically. Only the server or the
    network declining to answer earns a retry."""
    (rc, _o, _e), calls = _probe(monkeypatch, [(1, "", err)])
    assert rc == 1 and len(calls) == 1, (err, calls)


def test_every_live_probe_goes_through_the_retry(monkeypatch):
    """A probe added later that calls `_sh` directly would carry the old
    fragility silently. Assert the four live probes are routed."""
    import re as _re
    src = Path(M.__file__).read_text()
    live = [
        ('ls-remote", "--symref", "upstream", "HEAD"', "live upstream HEAD"),
        ('"--jq", ".parent.full_name // empty"', "upstream slug"),
        ("branches?per_page=100", "fork branch list"),
        ("gh\", \"api\", endpoint", "pin...branch compare"),
    ]
    for needle, what in live:
        i = src.find(needle.replace("\\", ""))
        assert i > 0, f"{what}: probe not found; this test is stale"
        window = src[max(0, i - 400):i]
        assert "_probe_with_transport_retry" in window or "_live_head_with_transport_retry" in window, (
            f"{what} still calls _sh directly: a transient failure there makes a "
            "fork unmeasured and refuses the whole release")
