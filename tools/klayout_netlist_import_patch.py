#!/usr/bin/env python3
"""Repair the base image's NetlistImportPlugin v0.3 (vibeic-eda#294).

Runtime source matches iic-jku/klayout-netlist-import tag v0.3, commit
15a783a9a6f7cc8efdcd708c017b0dde91bc9fd7. Only grain.xml has install metadata.
Quote the one prematurely evaluated forward reference; keep GUI autorun and
all parser behavior. Unknown source bytes require review, never a fuzzy patch.
This build-time edit preserves the original file's license, owner and mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

UPSTREAM_COMMIT = "15a783a9a6f7cc8efdcd708c017b0dde91bc9fd7"
ORIGINAL_SHA256 = "767febb8335a8724c07db3ccfe7ba9e09adba07ef4dbe7c39347cf1ad6e5429b"
PATCHED_SHA256 = "168191b7a28574e4fe5caf6c232289514342085a8c0e1fffaeac4a629e04adc7"
OLD = b"    def all_cells(self) -> List[NetlistCell]:\n"
NEW = b'    def all_cells(self) -> List["NetlistCell"]:\n'
DEFAULT_PARSER = Path(
    "/headless/.klayout/salt/NetlistImportPlugin/pymacros/netlist_parser.py")


def repair(source: bytes) -> bytes:
    digest = hashlib.sha256(source).hexdigest()
    if digest == PATCHED_SHA256:
        return source
    if digest != ORIGINAL_SHA256:
        raise ValueError(f"unrecognized NetlistImportPlugin parser SHA256: {digest}")
    if source.count(OLD) != 1:
        raise ValueError("expected exactly one all_cells forward reference")
    patched = source.replace(OLD, NEW, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError("patched parser does not match the reviewed bytes")
    return patched


def install(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular installed parser: {path}")
    source = path.read_bytes()
    patched = repair(source)
    if patched != source:
        # Build-time only: no running KLayout shares this filesystem layer.
        # Opening the existing file retains its owner and permission bits.
        path.write_bytes(patched)
    if path.read_bytes() != patched:
        raise ValueError("installed parser read-back differs from reviewed bytes")
    return {"plugin": "NetlistImportPlugin", "version": "0.3",
            "upstream_commit": UPSTREAM_COMMIT, "path": str(path),
            "sha256": PATCHED_SHA256,
            "status": "patched" if patched != source else "already_patched"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parser", type=Path, default=DEFAULT_PARSER)
    args = parser.parse_args()
    print(json.dumps(install(args.parser), sort_keys=True))


if __name__ == "__main__":
    main()
