"""The existing six-hex recipe identity, optionally including local inputs.

Opt in with one Dockerfile comment, relative to that Dockerfile's directory::

    # recipe-inputs: helper.py nested/another.py

Without this directive the digest is exactly the historical Dockerfile-only
SHA256 prefix. Declared paths and bytes are length-framed in sorted path order.
Invalid or unreadable declarations are BLOCKING: raise ValueError rather than
issuing a reusable tag that silently excludes a declared build input. This is
not COPY discovery; recipes must explicitly declare their local helper inputs.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

_DIRECTIVE = re.compile(rb"^[ \t]*#[ \t]*recipe-inputs\b([^\r\n]*)", re.M)
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


def recipe_digest(dockerfile: Path) -> str:
    """Return the recipe hash; fail closed for every invalid declared input."""
    data = dockerfile.read_bytes()
    digest = hashlib.sha256(data)
    directives = _DIRECTIVE.findall(data)
    if not directives:
        return digest.hexdigest()[:6]

    def invalid(reason):
        return ValueError(f"{dockerfile}: recipe-inputs: {reason}")

    if len(directives) != 1 or not directives[0].startswith(b":"):
        raise invalid("expected one '# recipe-inputs: path ...' directive")
    try:
        names = directives[0][1:].decode("ascii").split()
    except UnicodeDecodeError as error:
        raise invalid("paths must use portable ASCII names") from error
    if not names or len(set(names)) != len(names):
        raise invalid("input list is empty or contains duplicate paths")

    root = dockerfile.parent.resolve()
    digest.update(b"\0recipe-inputs:v1\0")
    for name in sorted(names):
        parts = PurePosixPath(name).parts
        if (not _PATH.fullmatch(name) or any(p in (".", "..") for p in name.split("/"))
                or name == dockerfile.name):
            raise invalid(f"not a canonical relative helper path: {name!r}")
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise invalid(f"symlink input is not supported: {name!r}")
        try:
            path.resolve(strict=True).relative_to(root)
            if not path.is_file():
                raise invalid(f"not a regular file: {name!r}")
            content = path.read_bytes()
        except (OSError, ValueError) as error:
            raise invalid(f"cannot read local input {name!r}: {error}") from error
        for field in (name.encode("ascii"), content):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()[:6]
