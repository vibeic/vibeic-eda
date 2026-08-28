#!/usr/bin/env python3
"""Bind a supplied RTL tree to a validation-only runner re-entry.

The Vibe-IC dual-track path may replace a PROGRAM-authored candidate with an
AI-authored backup or repair.  Re-entering the normal runner at RTL generation
would overwrite those bytes.  Merely skipping generation is not enough either:
the acceptance side needs machine-readable evidence that the files reviewed
before the run are exactly the files present after the downstream gates.

``record`` snapshots the supplied RTL before re-entry.  ``verify`` checks that
the file set and bytes are unchanged, the latest ``rtl_gen`` report says
``SKIPPED-BY-ENTRY`` for the declared entry step, and at least one downstream
step was actually dispatched.  This tool knows nothing about benchmark answers,
testbenches, or scores; it is a provenance boundary around ordinary EDA gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


SCHEMA = "vibeic.rtl_validation_reentry.v1"
SOURCES = {"AI_BACKUP", "AI_REPAIR", "USER_SUPPLIED"}


class ContractError(RuntimeError):
    """The supplied-RTL re-entry contract was not established."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(project: Path) -> list[dict[str, Any]]:
    project = project.resolve()
    rtl_dir = project / "phase2" / "stage1" / "rtl"
    if not rtl_dir.is_dir():
        raise ContractError(f"supplied RTL directory is absent: {rtl_dir}")
    paths = sorted({*rtl_dir.glob("*.v"), *rtl_dir.glob("*.sv")})
    if not paths:
        raise ContractError(f"supplied RTL directory is empty: {rtl_dir}")
    rows = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"RTL must be a regular, non-symlink file: {path}")
        rows.append({
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return rows


def _aggregate(files: list[dict[str, Any]]) -> str:
    body = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def record(project: Path, source: str, entry_step: int) -> dict[str, Any]:
    if source not in SOURCES:
        raise ContractError(f"source must be one of {sorted(SOURCES)}")
    if entry_step < 2:
        raise ContractError("validation-only re-entry must begin at step 2 or later")
    files = snapshot(project)
    return {
        "schema": SCHEMA,
        "source": source,
        "entry_step": entry_step,
        "project": str(project.resolve()),
        "rtl_files": files,
        "rtl_aggregate_sha256": _aggregate(files),
    }


def verify(project: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    project = project.resolve()
    if manifest.get("schema") != SCHEMA:
        raise ContractError(f"manifest schema must be {SCHEMA}")
    if Path(str(manifest.get("project") or "")).resolve() != project:
        raise ContractError("manifest project does not name the checked project")
    if manifest.get("source") not in SOURCES:
        raise ContractError("manifest source is not a supplied-RTL source")
    try:
        entry_step = int(manifest["entry_step"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("manifest entry_step is absent or invalid") from exc
    if entry_step < 2:
        raise ContractError("manifest would re-enter at RTL generation")

    before = manifest.get("rtl_files")
    after = snapshot(project)
    if before != after or manifest.get("rtl_aggregate_sha256") != _aggregate(after):
        raise ContractError("supplied RTL file set or bytes changed during re-entry")

    report_path = project / "reports" / "orchestrator" / "phase2_one_shot.json"
    try:
        report = json.loads(report_path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"runner report is absent or unreadable: {report_path}") from exc
    steps = report.get("steps")
    if not isinstance(steps, list):
        raise ContractError("runner report has no steps list")
    rtl_indices = [i for i, step in enumerate(steps)
                   if isinstance(step, dict) and step.get("name") == "rtl_gen"]
    if not rtl_indices:
        raise ContractError("runner report does not state the rtl_gen verdict")
    rtl_index = rtl_indices[-1]
    rtl_gen = steps[rtl_index]
    if rtl_gen.get("status") != "SKIPPED-BY-ENTRY":
        raise ContractError("rtl_gen was not SKIPPED-BY-ENTRY")
    detail = str(rtl_gen.get("detail") or "")
    if f"--entry-step {entry_step}" not in detail:
        raise ContractError("rtl_gen report is not bound to the declared entry step")
    downstream = [step for step in steps[rtl_index + 1:] if isinstance(step, dict)]
    dispatched = [step for step in downstream
                  if step.get("status") != "SKIPPED-BY-ENTRY"]
    if not dispatched:
        raise ContractError("no downstream PROGRAM gate was dispatched")

    return {
        **manifest,
        "status": "VERIFIED",
        "rtl_gen_status": rtl_gen.get("status"),
        "downstream_steps": len(downstream),
        "downstream_dispatched": len(dispatched),
        "runner_verdict": report.get("verdict"),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"manifest is absent or unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError("manifest must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="snapshot supplied RTL before re-entry")
    rec.add_argument("--project", required=True, type=Path)
    rec.add_argument("--source", required=True, choices=sorted(SOURCES))
    rec.add_argument("--entry-step", required=True, type=int)
    rec.add_argument("--manifest", required=True, type=Path)
    val = sub.add_parser("verify", help="verify bytes and runner provenance")
    val.add_argument("--project", required=True, type=Path)
    val.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            result = record(args.project, args.source, args.entry_step)
            _write_json(args.manifest, result)
        else:
            result = verify(args.project, _read_json(args.manifest))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ContractError as exc:
        print(f"rtl_reentry_guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
