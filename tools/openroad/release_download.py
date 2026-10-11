"""Checksum-verified release downloads with fresh, bounded redirect leases."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid


class DownloadError(RuntimeError):
    pass


def digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, sha256: str, dest: Path, *, attempts: int = 60,
             max_time: int = 240) -> Path:
    """Resume via the original URL, never a saved expiring CDN redirect.

    curl's -C - validates HTTP range support before appending. A server that
    ignores Range produces rc 33; restart rather than concatenate a full body.
    Only a complete SHA-256 match is promoted to the distdir filename. A failed
    attempt may leave resumable bytes, but cannot become a trusted cache entry.
    """
    dest = Path(dest)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("download requires an HTTPS URL without credentials")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("download requires a lowercase SHA-256 digest")
    if attempts < 1 or not 1 <= max_time <= 240:
        raise ValueError("attempts must be positive and max_time must be 1..240 seconds")
    if digest(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        if digest(partial) == sha256:
            os.replace(partial, dest)
            return dest
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k != "_vibeic_fetch"]
        query.append(("_vibeic_fetch", uuid.uuid4().hex))
        fresh = urlunsplit(parts._replace(query=urlencode(query), fragment=""))
        offset = partial.stat().st_size if partial.exists() else 0
        print(f"fetch {dest.name}: attempt {attempt}/{attempts}, resume {offset}",
              file=sys.stderr, flush=True)
        result = subprocess.run([
            "curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "20", "--max-time", str(max_time),
            "--retry", "0", "--continue-at", "-", "--output", str(partial),
            "--write-out", "%{http_code}", fresh,
        ], check=False, stdout=subprocess.PIPE, text=True)
        if digest(partial) == sha256:
            os.replace(partial, dest)
            return dest
        if (result.returncode in (0, 33)
                or (result.returncode == 22 and (result.stdout or "").strip() == "416")):
            # A completed but corrupt body, or unsupported resume, is not a
            # prefix we can safely continue. HTTP 416 also rejects an oversize
            # checkpoint. This is our private scratch file.
            partial.unlink(missing_ok=True)
        print(f"fetch {dest.name}: rc={result.returncode}, checksum not yet verified",
              file=sys.stderr, flush=True)
    raise DownloadError(f"{dest.name}: no verified download after {attempts} attempts")
