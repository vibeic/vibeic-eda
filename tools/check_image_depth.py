#!/usr/bin/env python3
"""Keep a release image within Docker's pullable layer-depth limit.

Docker's graph-driver pull path accepts at most 125 parent layers. A locally
composed image can exceed that limit and still build, run, and push: 0.3.0 did
exactly that with 126 layers. With ``--compact``, this program automatically
flattens an over-depth local candidate, restores and verifies its runtime image
configuration, and retags it in place before any version or ``latest`` tag is
pushed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fork-gatekeeper"))
import progress_watchdog as watchdog  # noqa: E402


DEFAULT_MAX_LAYERS = 125
DEFAULT_COMPACTION_STALL_GRACE_S = 1800.0
DEFAULT_COMPACTION_POLL_S = 5.0


class CompactionError(RuntimeError):
    """The candidate could not be compacted without changing its contract."""


class CompactionStalled(CompactionError):
    """The byte/command progress lease expired; no image verdict was formed."""


def _run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def layer_count(image: str) -> int:
    proc = _run(
        ["docker", "image", "inspect", image, "--format", "{{len .RootFS.Layers}}"],
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "docker image inspect failed"
        raise RuntimeError(detail)
    value = proc.stdout.strip()
    try:
        count = int(value)
    except ValueError as exc:
        raise RuntimeError(f"docker returned a non-integer layer count: {value!r}") from exc
    if count < 1:
        raise RuntimeError(f"docker returned an impossible layer count: {count}")
    return count


def image_details(image: str) -> tuple[dict[str, Any], str]:
    proc = _run([
        "docker", "image", "inspect", image, "--format",
        "{{json .Config}}|{{.Os}}/{{.Architecture}}",
    ])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "docker image inspect failed"
        raise CompactionError(f"cannot inspect candidate configuration: {detail}")
    try:
        raw_config, platform = proc.stdout.strip().rsplit("|", 1)
        config = json.loads(raw_config)
    except (ValueError, json.JSONDecodeError) as exc:
        raise CompactionError(
            f"docker returned malformed image configuration: {proc.stdout.strip()!r}"
        ) from exc
    if not isinstance(config, dict) or "/" not in platform:
        raise CompactionError("docker returned an incomplete image configuration")
    return config, platform


def _runtime_contract(config: dict[str, Any]) -> dict[str, Any]:
    """Return the image settings that affect consumers, with stable defaults."""
    return {
        "User": config.get("User") or "",
        "Env": config.get("Env") or [],
        "Entrypoint": config.get("Entrypoint"),
        "Cmd": config.get("Cmd"),
        "WorkingDir": config.get("WorkingDir") or "",
        "ExposedPorts": sorted((config.get("ExposedPorts") or {}).keys()),
        "Labels": config.get("Labels") or {},
        "OnBuild": config.get("OnBuild") or [],
        "Volumes": sorted((config.get("Volumes") or {}).keys()),
        "Healthcheck": config.get("Healthcheck"),
        "StopSignal": config.get("StopSignal") or "",
        "Shell": config.get("Shell"),
    }


def _config_changes(config: dict[str, Any]) -> list[str]:
    """Translate the supported runtime contract into docker-commit changes."""
    unsupported = []
    for key in ("Healthcheck", "StopSignal", "Shell"):
        if config.get(key):
            unsupported.append(key)
    # `docker export` omits mounted volume contents. Refuse rather than silently
    # flattening a filesystem with holes in it.
    if config.get("Volumes"):
        unsupported.append("Volumes")
    if unsupported:
        raise CompactionError(
            "automatic compaction cannot losslessly preserve: " + ", ".join(unsupported)
        )

    changes: list[str] = []
    for entry in config.get("Env") or []:
        if "=" not in entry:
            raise CompactionError(f"malformed ENV entry: {entry!r}")
        key, value = entry.split("=", 1)
        changes.append(f"ENV {key}={json.dumps(value)}")
    for port in sorted((config.get("ExposedPorts") or {}).keys()):
        changes.append(f"EXPOSE {port}")
    for key, value in sorted((config.get("Labels") or {}).items()):
        changes.append(f"LABEL {json.dumps(str(key))}={json.dumps(str(value))}")
    if config.get("Entrypoint") is not None:
        changes.append("ENTRYPOINT " + json.dumps(config["Entrypoint"], separators=(",", ":")))
    if config.get("Cmd") is not None:
        changes.append("CMD " + json.dumps(config["Cmd"], separators=(",", ":")))
    if config.get("WorkingDir"):
        changes.append("WORKDIR " + json.dumps(config["WorkingDir"]))
    if config.get("User"):
        changes.append(f"USER {config['User']}")
    for instruction in config.get("OnBuild") or []:
        changes.append(f"ONBUILD {instruction}")
    return changes


def _checked(args: list[str], *, timeout: int = 120) -> str:
    proc = _run(args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "command failed without output"
        raise CompactionError(f"{' '.join(args[:3])} failed: {detail}")
    return proc.stdout.strip()


class _Pipeline:
    """The export and import processes as one waitable supervision subject."""

    def __init__(self, exporter, importer) -> None:
        self.exporter = exporter
        self.importer = importer

    def poll(self):
        export_rc = self.exporter.poll()
        import_rc = self.importer.poll()
        if export_rc is None or import_rc is None:
            return None
        return import_rc if import_rc != 0 else export_rc

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("docker export | docker import", timeout)
            time.sleep(0.02)


def _kill_pipeline(pipeline: _Pipeline, reason: str) -> None:
    watchdog.terminate_process_group(pipeline.importer, reason)
    watchdog.terminate_process_group(pipeline.exporter, reason)


def _export_import(
    container: str,
    target: str,
    platform: str,
    *,
    stall_grace_s: float,
    poll_s: float = DEFAULT_COMPACTION_POLL_S,
) -> None:
    """Stream rootfs into one layer, leasing on bytes actually transferred."""
    with (tempfile.TemporaryFile() as export_err_file,
          tempfile.TemporaryFile() as import_out_file,
          tempfile.TemporaryFile() as import_err_file):
        exporter = subprocess.Popen(
            ["docker", "container", "export", container],
            stdout=subprocess.PIPE, stderr=export_err_file,
            start_new_session=True,
        )
        importer = subprocess.Popen(
            ["docker", "image", "import", "--platform", platform, "-", target],
            stdin=subprocess.PIPE, stdout=import_out_file, stderr=import_err_file,
            start_new_session=True,
        )
        assert exporter.stdout is not None and importer.stdin is not None
        transferred = 0
        pump_error: list[str] = []

        def pump() -> None:
            nonlocal transferred
            try:
                while True:
                    # os.read returns the bytes currently available; buffered
                    # ``read(1 MiB)`` may wait for the whole request or EOF and
                    # would hide a slowly advancing 31 GB stream from the lease.
                    chunk = os.read(exporter.stdout.fileno(), 1024 * 1024)
                    if not chunk:
                        break
                    # Count bytes as the kernel accepts them, not only after a
                    # whole 1 MiB Python write returns. A slow but healthy
                    # importer can otherwise move pipe-sized pieces for an
                    # entire grace window while this counter appears flat.
                    pending = memoryview(chunk)
                    while pending:
                        wrote = os.write(importer.stdin.fileno(), pending[:64 * 1024])
                        if wrote <= 0:
                            raise BrokenPipeError("docker import accepted zero bytes")
                        transferred += wrote
                        pending = pending[wrote:]
            except (BrokenPipeError, OSError) as exc:
                pump_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    importer.stdin.close()
                except OSError:
                    pass

        thread = threading.Thread(target=pump, name="image-rootfs-pump", daemon=True)
        thread.start()
        pipeline = _Pipeline(exporter, importer)
        meter = watchdog.ProgressMeter(size_fn=lambda: transferred)
        outcome, _ = watchdog.supervise(
            pipeline, meter.sample, _kill_pipeline,
            poll_s=poll_s, stall_grace_s=stall_grace_s,
        )
        thread.join(timeout=10)
        for proc in (exporter, importer):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watchdog.terminate_process_group(proc, "cleanup")

        def read(file) -> bytes:
            file.seek(0)
            return file.read()

        detail = b"\n".join((
            read(export_err_file), read(import_err_file), read(import_out_file)
        )).decode(errors="replace").strip()
        if outcome == "stalled":
            raise CompactionStalled(
                "filesystem compaction stalled with no transferred rootfs bytes "
                f"for > {stall_grace_s:g}s; total runtime was not bounded"
            )
        if pump_error or exporter.returncode != 0 or importer.returncode != 0:
            extra = "; ".join(pump_error)
            raise CompactionError(
                "filesystem compaction failed: " + (detail or extra or "no diagnostic")
            )


def compact_image(
    image: str,
    max_layers: int,
    *,
    stall_grace_s: float = DEFAULT_COMPACTION_STALL_GRACE_S,
) -> int:
    """Flatten ``image``, preserve its runtime contract, then retag it."""
    original_config, platform = image_details(image)
    changes = _config_changes(original_config)
    token = uuid.uuid4().hex[:12]
    export_container = f"vibeic-depth-export-{token}"
    config_container = f"vibeic-depth-config-{token}"
    raw_tag = f"vibeic-depth-compact:{token}-rootfs"
    configured_tag = f"vibeic-depth-compact:{token}-configured"

    try:
        _checked([
            "docker", "container", "create", "--name", export_container,
            "--entrypoint", "/bin/true", image,
        ])
        _export_import(
            export_container, raw_tag, platform,
            stall_grace_s=stall_grace_s,
        )
        _checked([
            "docker", "container", "create", "--name", config_container,
            raw_tag, "/bin/true",
        ])
        commit = ["docker", "container", "commit"]
        for change in changes:
            commit.extend(["--change", change])
        commit.extend([config_container, configured_tag])
        committed = watchdog.run_supervised(
            commit, stall_grace_s=stall_grace_s,
            process_tree_cpu_progress=True,
        )
        if committed.rc != 0:
            detail = (committed.err or committed.out).strip()
            error = CompactionStalled if committed.stalled else CompactionError
            raise error(f"docker container commit failed: {detail}")

        compacted_config, _ = image_details(configured_tag)
        before = _runtime_contract(original_config)
        after = _runtime_contract(compacted_config)
        if after != before:
            differing = {
                key: {"before": before[key], "after": after[key]}
                for key in before
                if before[key] != after[key]
            }
            raise CompactionError(
                "compacted image runtime configuration differs from candidate: "
                + json.dumps(differing, sort_keys=True)
            )
        compacted_layers = layer_count(configured_tag)
        if compacted_layers > max_layers:
            raise CompactionError(
                f"compacted image still has {compacted_layers} layers (limit {max_layers})"
            )
        # This is the first mutation of the caller's tag. Every operation and
        # verification above leaves the original candidate untouched on failure.
        _checked(["docker", "image", "tag", configured_tag, image])
        return compacted_layers
    finally:
        for container in (config_container, export_container):
            try:
                _run(["docker", "container", "rm", "-f", container])
            except (OSError, subprocess.SubprocessError):
                pass
        for temp_tag in (configured_tag, raw_tag):
            try:
                _run(["docker", "image", "rm", "-f", temp_tag])
            except (OSError, subprocess.SubprocessError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="local image reference to inspect before publish")
    parser.add_argument(
        "--max-layers",
        type=int,
        default=DEFAULT_MAX_LAYERS,
        help=f"maximum pullable layer depth (default: {DEFAULT_MAX_LAYERS})",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="automatically flatten and retag an over-depth local candidate",
    )
    parser.add_argument(
        "--stall-grace",
        type=float,
        default=DEFAULT_COMPACTION_STALL_GRACE_S,
        help="seconds with zero transferred bytes before compaction is stalled; "
             "this is not a total runtime limit",
    )
    args = parser.parse_args(argv)
    if args.max_layers < 1:
        parser.error("--max-layers must be positive")
    if args.stall_grace <= 0:
        parser.error("--stall-grace must be positive")

    try:
        count = layer_count(args.image)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"[REFUSED] cannot prove image depth for {args.image}: {exc}", file=sys.stderr)
        return 2

    if count > args.max_layers:
        if args.compact:
            try:
                compacted = compact_image(
                    args.image, args.max_layers,
                    stall_grace_s=args.stall_grace,
                )
            except CompactionStalled as exc:
                print(
                    f"[STALLED] automatic compaction made no forward progress "
                    f"for {args.image}: {exc}",
                    file=sys.stderr,
                )
                return watchdog.RC_STALLED
            except (CompactionError, RuntimeError, subprocess.SubprocessError) as exc:
                print(
                    f"[REFUSED] automatic compaction failed for {args.image}: {exc}",
                    file=sys.stderr,
                )
                return 2
            print(
                f"[COMPACTED] {args.image}: {count} -> {compacted}/{args.max_layers} "
                "pullable layers"
            )
            return 0
        print(
            f"[REFUSED] {args.image} has {count} layers; Docker can pull at most "
            f"{args.max_layers}. Compact the Dockerfile and rebuild before publishing.",
            file=sys.stderr,
        )
        return 1

    print(f"[OK] {args.image}: {count}/{args.max_layers} pullable layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
