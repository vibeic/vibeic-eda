"""Bounded release downloads must refresh leases and promote only verified bytes.

The production API is exercised through its subprocess.run seam. A separate
loopback-only HTTP server checks real curl's redirect/error/range behavior; it
does not weaken the downloader's HTTPS-only production policy or contact GitHub.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_download_under_test", ROOT / "tools/openroad/release_download.py")
D = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = D
SPEC.loader.exec_module(D)
URL = "https://github.com/example/tool/releases/download/v1/archive.tar.zst"
PAYLOAD = b"neutral release archive contents\x00\x01\x02\xff" * 17
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class ScriptedCurl:
    """Write curl-like resumable bytes, while auditing every exact invocation."""

    def __init__(self, monkeypatch, steps):
        self.steps = list(steps)
        self.calls = []
        monkeypatch.setattr(D.subprocess, "run", self)

    def __call__(self, command, **kwargs):
        assert self.steps, "unexpected additional curl attempt"
        assert command[0] == "curl"
        assert "-fsSL" in command, "HTTP error/redirect bodies must not become archive bytes"
        for flag, value in (("--proto", "=https"), ("--proto-redir", "=https"),
                            ("--continue-at", "-"), ("--retry", "0"),
                            ("--connect-timeout", "20")):
            assert command[command.index(flag) + 1] == value
        assert 1 <= int(command[command.index("--max-time") + 1]) <= 240
        assert kwargs.get("check") is False
        output = Path(command[command.index("--output") + 1])
        prior = output.read_bytes() if output.exists() else b""
        self.calls.append({"url": command[-1], "prior": prior,
                           "output": output, "command": command})
        step = self.steps.pop(0)
        assert prior == step.get("prior", b"")
        if "observe" in step:
            step["observe"]()
        if "append" in step:
            with output.open("ab") as stream:
                stream.write(step["append"])
        if "replace" in step:
            output.write_bytes(step["replace"])
        return subprocess.CompletedProcess(command, step["rc"], stdout=step.get("http_code"))


def test_expired_618_retry_refreshes_original_url_not_signed_redirect(tmp_path, monkeypatch):
    fake = ScriptedCurl(monkeypatch, [{"rc": 22}, {"rc": 0, "append": PAYLOAD}])
    dest = tmp_path / "archive.tar.zst"
    original = URL + "?download=1&empty=&_vibeic_fetch=old#fragment"
    assert D.download(original, SHA, dest, attempts=2) == dest
    assert dest.read_bytes() == PAYLOAD
    assert len(fake.calls) == 2
    queries = []
    for call in fake.calls:
        parsed = urlsplit(call["url"])
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "github.com", urlsplit(URL).path)
        assert parsed.fragment == ""
        query = parse_qs(parsed.query, keep_blank_values=True)
        assert query["download"] == ["1"] and query["empty"] == [""]
        assert len(query["_vibeic_fetch"]) == 1
        assert query["_vibeic_fetch"] != ["old"]
        queries.append(query["_vibeic_fetch"][0])
    assert len(set(queries)) == 2


def test_timeout_partial_resumes_without_early_promotion(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    prefix = PAYLOAD[:37]
    def unpromoted():
        assert not dest.exists()
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 28, "append": prefix, "observe": unpromoted},
        {"rc": 0, "prior": prefix, "append": PAYLOAD[37:], "observe": unpromoted},
    ])
    assert D.download(URL, SHA, dest, attempts=2, max_time=7) == dest
    assert dest.read_bytes() == PAYLOAD
    assert not dest.with_name("archive.part").exists()
    assert len({c["url"] for c in fake.calls}) == 2
    assert all(c["command"][c["command"].index("--max-time") + 1] == "7" for c in fake.calls)


def test_618_during_resume_preserves_contiguous_partial(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    prefix = PAYLOAD[:51]
    dest.with_name("archive.part").write_bytes(prefix)
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 22, "http_code": "618", "prior": prefix},
        {"rc": 0, "prior": prefix, "append": PAYLOAD[51:]},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD
    assert len(fake.calls) == 2


def test_ignored_range_rc33_restarts_instead_of_concatenating_full_body(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    prefix = PAYLOAD[:23]
    dest.with_name("archive.part").write_bytes(prefix)
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 33, "prior": prefix},
        {"rc": 0, "prior": b"", "append": PAYLOAD},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD
    assert [c["prior"] for c in fake.calls] == [prefix, b""]


def test_416_oversized_partial_is_reset_before_fresh_complete_download(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    oversized = b"x" * (len(PAYLOAD) + 23)
    dest.with_name("archive.part").write_bytes(oversized)
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 22, "http_code": "416", "prior": oversized},
        {"rc": 0, "http_code": "200", "prior": b"", "append": PAYLOAD},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD
    assert [c["prior"] for c in fake.calls] == [oversized, b""]


def test_416_then_failed_retry_never_promotes_or_reuses_oversized_bytes(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    oversized = b"x" * (len(PAYLOAD) + 23)
    dest.with_name("archive.part").write_bytes(oversized)
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 22, "http_code": "416", "prior": oversized},
        {"rc": 28, "http_code": "200", "prior": b"", "append": PAYLOAD[:19]},
    ])
    with pytest.raises(D.DownloadError):
        D.download(URL, SHA, dest, attempts=2)
    assert not dest.exists()
    assert dest.with_name("archive.part").read_bytes() == PAYLOAD[:19]
    assert len(fake.calls) == 2


@pytest.mark.parametrize("body", [b"x" * len(PAYLOAD), b"<html>jwt:expired</html>"])
def test_complete_corrupt_body_never_promoted_even_if_same_size(tmp_path, monkeypatch, body):
    dest = tmp_path / "archive"
    fake = ScriptedCurl(monkeypatch, [{"rc": 0, "append": body}])
    with pytest.raises(D.DownloadError):
        D.download(URL, SHA, dest, attempts=1)
    assert not dest.exists()
    assert not dest.with_name("archive.part").exists()
    assert len(fake.calls) == 1


def test_corrupt_complete_response_is_discarded_before_retry(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 0, "append": b"x" * len(PAYLOAD)},
        {"rc": 0, "prior": b"", "append": PAYLOAD},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD
    assert len(fake.calls) == 2


def test_untrusted_existing_partial_cannot_poison_promoted_archive(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    wrong_prefix = b"x" * 19
    dest.with_name("archive.part").write_bytes(wrong_prefix)
    ScriptedCurl(monkeypatch, [
        {"rc": 0, "prior": wrong_prefix, "append": PAYLOAD[19:]},
        {"rc": 0, "prior": b"", "append": PAYLOAD},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD


@pytest.mark.parametrize("rc", [0, 18, 28])
def test_full_verified_bytes_can_be_accepted_despite_curl_transport_error(tmp_path, monkeypatch, rc):
    dest = tmp_path / "archive"
    fake = ScriptedCurl(monkeypatch, [{"rc": rc, "append": PAYLOAD}])
    assert D.download(URL, SHA, dest, attempts=1) == dest
    assert dest.read_bytes() == PAYLOAD
    assert len(fake.calls) == 1


def test_valid_existing_destination_skips_network(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    dest.write_bytes(PAYLOAD)
    fake = ScriptedCurl(monkeypatch, [])
    assert D.download(URL, SHA, dest) == dest
    assert fake.calls == []


def test_valid_existing_partial_is_atomically_promoted_without_network(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    partial = dest.with_name("archive.part")
    partial.write_bytes(PAYLOAD)
    fake = ScriptedCurl(monkeypatch, [])
    assert D.download(URL, SHA, dest) == dest
    assert dest.read_bytes() == PAYLOAD
    assert not partial.exists()
    assert fake.calls == []


def test_invalid_destination_remains_unchanged_until_verified_replacement(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    dest.write_bytes(b"old invalid file")
    def unchanged():
        assert dest.read_bytes() == b"old invalid file"
    ScriptedCurl(monkeypatch, [
        {"rc": 28, "append": PAYLOAD[:31], "observe": unchanged},
        {"rc": 0, "prior": PAYLOAD[:31], "append": PAYLOAD[31:], "observe": unchanged},
    ])
    D.download(URL, SHA, dest, attempts=2)
    assert dest.read_bytes() == PAYLOAD


@pytest.mark.parametrize("preexisting", [False, True])
def test_exhaustion_retains_only_untrusted_partial_never_promotes(tmp_path, monkeypatch, preexisting):
    dest = tmp_path / "archive"
    if preexisting:
        dest.write_bytes(b"previous invalid archive")
    fake = ScriptedCurl(monkeypatch, [
        {"rc": 28, "append": PAYLOAD[:9]},
        {"rc": 28, "prior": PAYLOAD[:9], "append": PAYLOAD[9:18]},
        {"rc": 28, "prior": PAYLOAD[:18], "append": PAYLOAD[18:27]},
    ])
    with pytest.raises(D.DownloadError, match="3"):
        D.download(URL, SHA, dest, attempts=3)
    assert len(fake.calls) == 3
    assert dest.with_name("archive.part").read_bytes() == PAYLOAD[:27]
    if preexisting:
        assert dest.read_bytes() == b"previous invalid archive"
    else:
        assert not dest.exists()


@pytest.mark.parametrize("url", ["http://github.com/archive", "file:///tmp/archive",
                                  "https://user:secret@example.invalid/a", "https:///a"])
def test_non_https_or_credential_url_is_rejected_before_network(tmp_path, monkeypatch, url):
    fake = ScriptedCurl(monkeypatch, [])
    with pytest.raises(ValueError):
        D.download(url, SHA, tmp_path / "archive")
    assert fake.calls == []


@pytest.mark.parametrize("sha", ["0" * 63, "g" * 64, SHA.upper(), ""])
def test_invalid_expected_sha_is_rejected_before_network(tmp_path, monkeypatch, sha):
    fake = ScriptedCurl(monkeypatch, [])
    with pytest.raises(ValueError):
        D.download(URL, sha, tmp_path / "archive")
    assert fake.calls == []


@pytest.mark.parametrize("kwargs", [{"attempts": 0}, {"attempts": -1},
                                   {"max_time": 0}, {"max_time": 241}])
def test_unbounded_or_invalid_attempt_parameters_are_rejected(tmp_path, monkeypatch, kwargs):
    fake = ScriptedCurl(monkeypatch, [])
    with pytest.raises(ValueError):
        D.download(URL, SHA, tmp_path / "archive", **kwargs)
    assert fake.calls == []


@pytest.fixture
def http_fixture():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append((path, self.headers.get("Range")))
            body = PAYLOAD
            headers = {}
            status = 200
            if path == "/redirect":
                status, body = 302, b"POISON redirect response body"
                headers["Location"] = "/complete"
            elif path == "/redirect-expired":
                status, body = 302, b"POISON redirect response body"
                headers["Location"] = "/expired"
            elif path == "/expired":
                status, body = 618, b"jwt:expired POISON"
            elif path == "/unsatisfiable":
                status, body = 416, b"Range not satisfiable POISON"
                headers["Content-Range"] = f"bytes */{len(PAYLOAD)}"
            elif path == "/range":
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1][:-1])
                status, body = 206, PAYLOAD[offset:]
                headers["Content-Range"] = f"bytes {offset}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # curl rejects a bad status/range before reading its body.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def real_curl(url, dest):
    curl = shutil.which("curl")
    assert curl, "curl is required by the production release downloader"
    # HTTP is allowed only in this direct, loopback curl-semantics test. The
    # production downloader's HTTPS guard is separately exercised above.
    return subprocess.run([
        curl, "-fsSL", "--noproxy", "*", "--proto", "=http", "--proto-redir", "=http",
        "--connect-timeout", "2", "--max-time", "5", "--retry", "0",
        "--continue-at", "-", "--write-out", "%{http_code}", "--output", str(dest), url,
    ], capture_output=True, timeout=10, check=False)


def test_real_curl_range_ignored_returns_33_without_appending_full_body(tmp_path, http_fixture):
    base, requests = http_fixture
    partial = tmp_path / "archive.part"
    partial.write_bytes(PAYLOAD[:29])
    result = real_curl(base + "/complete", partial)
    assert result.returncode == 33, result.stderr
    assert partial.read_bytes() == PAYLOAD[:29]
    assert requests == [("/complete", "bytes=29-")]


def test_real_curl_valid_range_appends_only_requested_suffix(tmp_path, http_fixture):
    base, requests = http_fixture
    partial = tmp_path / "archive.part"
    partial.write_bytes(PAYLOAD[:29])
    result = real_curl(base + "/range", partial)
    assert result.returncode == 0, result.stderr
    assert partial.read_bytes() == PAYLOAD
    assert requests == [("/range", "bytes=29-")]


def test_real_curl_redirect_body_is_not_part_of_download(tmp_path, http_fixture):
    base, requests = http_fixture
    partial = tmp_path / "archive.part"
    result = real_curl(base + "/redirect", partial)
    assert result.returncode == 0, result.stderr
    assert partial.read_bytes() == PAYLOAD
    assert [path for path, _ in requests] == ["/redirect", "/complete"]


def test_real_curl_redirect_to_618_does_not_contaminate_partial(tmp_path, http_fixture):
    base, requests = http_fixture
    partial = tmp_path / "archive.part"
    partial.write_bytes(PAYLOAD[:29])
    result = real_curl(base + "/redirect-expired", partial)
    assert result.returncode == 22, result.stderr
    assert partial.read_bytes() == PAYLOAD[:29]
    assert [path for path, _ in requests] == ["/redirect-expired", "/expired"]


def test_real_curl_416_can_report_success_but_does_not_verify_partial(tmp_path, http_fixture):
    base, requests = http_fixture
    partial = tmp_path / "archive.part"
    oversized = b"x" * (len(PAYLOAD) + 23)
    partial.write_bytes(oversized)
    result = real_curl(base + "/unsatisfiable", partial)
    # Real curl -C - regards this 416 as an already-complete transfer and
    # returns zero. Only SHA verification distinguishes our corrupt oversized
    # partial from a valid download; HTTP/transport success is insufficient.
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"416"
    assert partial.read_bytes() == oversized
    assert requests == [("/unsatisfiable", f"bytes={len(oversized)}-")]
