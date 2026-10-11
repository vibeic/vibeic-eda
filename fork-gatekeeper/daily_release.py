#!/usr/bin/env python3
"""Move every pin to its fork's tip, rebuild what changed, and cut a new image.

OWNER RULING, 2026-07-29
========================
    "只要有任何工具有新版本（有我們自己的改動，或者是 upstream merge 進來的
     commit），該工具就要重新 build 一次。只要有任何一個工具有新版，我們的
     image 也就重新再出一版。只要有任何新的 update，我們最晚會在隔天早上的
     5 點半，提供一個 daily 的新版 updated docker image。"

`daily_merge.py` takes upstream's commits into the forks. That is one hop of
three, and the other two are where the work has historically been lost:

    fork branch  ->  pin  ->  rebuilt artefact  ->  composed image

On 2026-07-29 `vibeic/yosys#2` was reviewed and merged and every run that day
still used the pre-merge yosys, because the pin never moved. The owner found it,
not a gate:

    "容器映像還沒重建，跑的還是舊 yosys -> WHY YOU DIDNT REBUILD!!!"

A merge that stops at the fork has changed nothing anyone runs. This program is
the rest of the chain, and it is deliberately the SAME program for both causes of
a new version — an upstream merge and one of our own commits are the same event
here, because the pin does not care which produced the tip.

WHAT IT REFUSES
===============
* It will not bump a pin it cannot resolve to a branch tip. A pin it cannot read
  is reported, never left alone silently.
* It will not cut an image version when no tool changed. A version number that
  moves without content is worse than no release — it makes "we shipped today"
  true and meaningless.
* It will not skip the multi-repo tag composition. `eda-tool-lvs` is tagged
  `<magic>-<netgen>`; bumping magic alone while leaving the tag would keep
  pulling the artefact built before the bump. The tag composition is READ from
  `docker-bake.hcl` rather than hard-coded, so a new pairing cannot drift.

WHAT IT DOES NOT PROVE
======================
That the rebuilt image works. It builds, and a build that fails stops the
release with the failure attached — but the regression suite is
`build_and_regress.sh`'s job, not this one's. A green build is not a green tool.

Exit: 0 released or nothing to do, 1 something needed a human, 2 nothing checked.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
from enum import Enum
import fcntl
import concurrent.futures
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from recipe_digest import recipe_digest                       # noqa: E402
from check_pins_current import (branch_is_ours, check_one,       # noqa: E402
                                pin_sites,
                                pinned_refs)
import progress_watchdog as watchdog                            # noqa: E402
from capability_smoke import run_container_probe               # noqa: E402

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: How many hex characters the artefact tag uses. `short()` in docker-bake.hcl.
SHORT = 7


def _release_lock_path() -> Path:
    """One host-wide lock for every checkout that can publish ``:latest``.

    The lock cannot live in the checkout: two clones are precisely how an old
    noop and a new release can otherwise run concurrently.  Resolve the account
    home from the uid database rather than HOME/XDG environment overrides, so
    every supported invocation by this release authority converges on the same
    inode.
    """
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return home / ".cache" / "eda-fork-gatekeeper" / "daily-release.lock"


def _acquire_release_lock(path: Path):
    """Acquire the release authority without waiting or following a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            raise OSError("release lock is not a private regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, (f"pid={os.getpid()} started={_utcnow()}\n").encode())
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


class Presence(str, Enum):
    """A measured image/tag state; UNKNOWN is never treated as absence."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PresenceMeasurement:
    state: Presence
    subject: str
    source: str
    detail: str = ""
    # A PRESENT answer says only that a tag resolves.  Publication checks also
    # have to prove that ``:latest`` resolves to the SAME manifest as the
    # immutable version tag; an old-but-present latest is the exact state left
    # by a failed latest push.  Registry probes fill this with a digest of the
    # canonical manifest JSON.  Other presence callers do not need it.
    identity: Optional[str] = None


#: How much of a tool's diagnostic is STORED -- in evidence, in an exception,
#: in a failure summary.  Classification always reads the full text first; this
#: bounds only what is kept.
#:
#: 2026-09-30: `docker manifest inspect` failed with "unsupported manifest
#: format: &{...}" followed by the whole deserialized manifest as a Go struct,
#: raw bytes printed as decimals -- 85,893 characters on ONE line (85,923 as
#: stored, with its prefix).  The exception carried all of it, the failure
#: summary carried it again, and the alert built from the record exceeded
#: GitHub's body limit.  The evidence dict next to it was already cut to
#: `[-1200:]`, but a tail alone kept only the byte dump and dropped the one
#: phrase that says what went wrong.  So the bound keeps BOTH ends: the first
#: line (what failed, capped, because it can be the whole text) and the last
#: characters (where the tool ended up).
DETAIL_HEAD_CHARS = 400
DETAIL_TAIL_CHARS = 1200
#: Room for the elision marker, so bounding a bounded text returns it unchanged.
_DETAIL_SLACK = 64


def _bounded_detail(text: Optional[str], *, head: int = DETAIL_HEAD_CHARS,
                    tail: int = DETAIL_TAIL_CHARS) -> str:
    """First line (at most ``head`` chars) + an elision marker + last ``tail``."""
    text = text or ""
    if len(text) <= head + tail + _DETAIL_SLACK:
        return text
    first = text.split("\n", 1)[0][:head]
    elided = len(text) - len(first) - tail
    return f"{first} ... [{elided} chars elided] ... {text[-tail:]}"


class ReleaseMeasurementUnknown(RuntimeError):
    """A release input could not be measured, so mutation must not proceed."""

    def __init__(self, *, stage: str, category: str, subject: str,
                 detail: str):
        self.stage = stage
        self.category = category
        self.subject = subject
        # Bounded here, once, for every raiser: this text becomes the
        # `unmeasured[]` entry, the failure summary and the [UNMEASURED] line.
        self.detail = _bounded_detail(detail)
        super().__init__(f"{subject}: {self.detail}")

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "category": self.category,
            "subject": self.subject,
            "detail": self.detail,
        }


def _strict_json_loads(raw: str):
    """Parse registry/git evidence without duplicate keys or non-finite values."""
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(raw, object_pairs_hook=object_pairs,
                      parse_constant=constant)


#: Environment every buildx invocation gets.
#:
#: buildkit truncates a single step's log at 2 MiB by default and says so only
#: in a line that is itself easy to miss. The ALIGN step blows past that — CBC
#: builds serially and is verbose — so exactly the step that has wedged four
#: times is the one whose log stops. `-1` removes the limit.
#:
#: BUT ONLY WHERE BUILDKIT READS IT, WHICH IS NOT HERE BY DEFAULT. The variable
#: is read by the buildkit DAEMON, not by the buildx client. With the `docker`
#: driver — the default, and what this host uses — buildkit lives inside
#: dockerd, so exporting it into the client process does exactly nothing.
#: Measured: the klayout build with this env in place still printed
#: `[output clipped, log limit 2MiB reached]`.
#:
#: It is kept because it IS the correct setting for the `docker-container`
#: driver, where buildx passes the env through to the builder container. What
#: is not kept is the silence: `log_limit_effective()` below reports when this
#: cannot work, so the setting cannot look like it is doing something.
BUILD_ENV = {"BUILDKIT_STEP_LOG_MAX_SIZE": "-1",
             "BUILDKIT_PROGRESS": "plain"}


#: A builder whose buildkit CAN honour BUILDKIT_STEP_LOG_MAX_SIZE.
#:
#: With the default `docker` driver, buildkit runs inside dockerd and never sees
#: the client's environment, so the variable is a no-op — measured, and the
#: klayout build printed `[output clipped, log limit 2MiB reached]` with it set.
#: The obvious fix is a dockerd drop-in plus a restart, and a restart here would
#: have killed a running ibex route, a long-lived container, and someone else's
#: Discourse. A `docker-container` builder lifts the limit for our builds and
#: touches nothing else:
#:
#:     docker buildx create --name vibeic-builder --driver docker-container \
#:         --driver-opt env.BUILDKIT_STEP_LOG_MAX_SIZE=-1 \
#:         --driver-opt env.BUILDKIT_STEP_LOG_MAX_SPEED=-1
#:
#: Measured on a step emitting 90 000 lines:
#:
#:     default builder    kept to line   3120,  1 clip message, 232 KB
#:     vibeic-builder     kept to line  90000,  0 clip messages, 6.6 MB
#:
#: Used when it exists, ignored when it does not — a release must not fail
#: because a builder was never created on this host.
PREFERRED_BUILDER = "vibeic-builder"


def preferred_builder() -> Optional[str]:
    """`PREFERRED_BUILDER` if it exists and is usable, else None."""
    rc, out, _ = _sh(["docker", "buildx", "inspect", PREFERRED_BUILDER],
                     timeout=120)
    if rc != 0 or "running" not in out.lower():
        return None
    return PREFERRED_BUILDER


def log_limit_effective(builder: Optional[str] = None) -> Optional[bool]:
    """Can BUILDKIT_STEP_LOG_MAX_SIZE actually take effect here?

    None when the driver cannot be read — not-known, which must not read as a
    pass. This is the second time in two days that a fix of mine changed the
    layer I control while the behaviour lived one layer further out (the first
    removed our COPY of a directory the BASE image ships). The cheap defence is
    the same both times: have the program say which layer it reached.

    Asks about the builder the release WILL USE, not whichever one is current.
    Those differ the moment a preferred builder exists, and reporting on the
    wrong one is the same class of error as the setting it reports on.
    """
    cmd = ["docker", "buildx", "inspect"] + ([builder] if builder else [])
    rc, out, _ = _sh(cmd, timeout=60)
    if rc != 0 or not out.strip():
        return None
    for line in out.splitlines():
        if line.lower().startswith("driver:"):
            return line.split(":", 1)[1].strip() != "docker"
    return None

#: A no-progress lease, not an estimate of how long a build should take. Output
#: bytes or scoped process-tree CPU renew it; total runtime has no ceiling.
#: BuildKit's plain progress stream makes completed vertices observable while
#: the process-tree probe covers local compiler work.
BUILD_STALL_GRACE_S = float(os.environ.get("GK_BUILD_STALL_GRACE", "1800"))
BUILD_POLL_S = float(os.environ.get("GK_BUILD_PROGRESS_POLL", "5"))
PUSH_STALL_GRACE_S = float(os.environ.get("GK_PUSH_STALL_GRACE", "900"))
SMOKE_STALL_GRACE_S = float(os.environ.get("GK_SMOKE_STALL_GRACE", "60"))
COMPACTION_STALL_GRACE_S = float(
    os.environ.get("GK_COMPACTION_STALL_GRACE", "1800")
)

#: A network outage in the registry compose must not turn a completely-built
#: release into tomorrow's work.  BuildKit cache makes these retries cheap: the
#: tool artefacts have already been pushed and unchanged compose stages are
#: reused.  Five attempts span eight minutes of DNS/transport recovery time,
#: while an unclassified (usually deterministic) failure still gets exactly the
#: one defence-in-depth retry the release had before this policy existed.
COMPOSE_TRANSIENT_ATTEMPTS = 5
COMPOSE_RETRY_DELAYS = (15, 45, 120, 300)
STREAM_TAIL_CHARS = 128 * 1024

_TRANSIENT_FAILURES = (
    ("dns", ("could not resolve host", "temporary failure in name resolution",
             "timeout while contacting dns servers", "server misbehaving")),
    ("network_timeout", ("i/o timeout", "connection timed out",
                         "tls handshake timeout", "context deadline exceeded")),
    ("network_reset", ("connection reset by peer", "unexpected eof",
                       "the remote end hung up unexpectedly", "rpc failed")),
    ("service_unavailable", ("http 429", "http 502", "http 503", "http 504",
                             "too many requests", "service unavailable")),
    ("progress_stall", ("watchdog_stalled",)),
    # A `docker manifest inspect` reply that does not describe the tag.  The
    # docker CLI (29.7.1, cli/command/manifest/inspect.go) asks GetManifest
    # first -- which for an OCI image manifest ALSO fetches the config blob
    # (internal/registryclient/fetcher.go:98) -- and on ANY error discards that
    # error and falls back to GetManifestList.  A single image manifest is not
    # a list, so fetchList answers "unsupported manifest format: %v" and prints
    # the manifest it successfully fetched (fetcher.go:54).  The message
    # therefore means "the first fetch failed for a reason the CLI threw away".
    # 2026-09-30 05:30: 0.3.88's read-back got this once, fully published; the
    # same command then returned rc 0 for 0.3.88 and :latest twelve times in a
    # row.  LAST in the table, so a network signature in the same text names
    # the cause instead.
    ("registry_manifest_unstable", ("unsupported manifest format",)),
)
_EXTENDED_RETRY_CATEGORIES = {
    "dns", "network_timeout", "network_reset", "service_unavailable",
    "registry_manifest_unstable",
}
#: Categories whose meaning comes from the REGISTRY READ-BACK command and only
#: from it.  The fallback that produces "unsupported manifest format" exists in
#: `docker manifest inspect`; in a build or compose log the same words would be
#: a manifest that genuinely does not parse, which waiting does not change.
#: `_build_failure_category` removes these, so a deterministic compose failure
#: keeps exactly the attempts, category and `retryable` it had before.
_READBACK_ONLY_CATEGORIES = frozenset({"registry_manifest_unstable"})

# Registry presence is a release-authority input, so an UNKNOWN answer must
# still fail closed.  A single TLS handshake timeout, however, is not evidence
# that an immutable artefact is absent.  Re-ask the same tag a bounded number
# of times before returning UNKNOWN; this mirrors the compose retry policy but
# never turns an unmeasured answer into an absence or a successful publication.
#
# Only the CLI's manifest fallback widens to five attempts over 102 s. Other
# transport errors retain three attempts over 12 s. How
# long the 2026-09-30 condition lasted was never measured -- that read was not
# retried at all, and the tag read healthy again minutes later -- so the window
# is widened toward that scale rather than fitted to it.  Exhausting it is
# still UNKNOWN, never PRESENT or ABSENT.
MANIFEST_TRANSIENT_ATTEMPTS = 3
MANIFEST_RETRY_DELAYS = (2, 10)
MANIFEST_UNSTABLE_ATTEMPTS = 5
MANIFEST_UNSTABLE_RETRY_DELAYS = (2, 10, 30, 60)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: str | Path, value: dict) -> None:
    """Replace a JSON sidecar atomically, including on a failed run.

    The old release wrote `--json` only at the bottom of the success path.  A
    failure therefore left yesterday's successful object at the live path.  A
    consumer could not distinguish "today passed" from "today returned before
    writing".  A running record is already more honest than that stale pass;
    every known exit below replaces it again with its terminal state.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _finish(result: dict, json_path: Optional[str], rc: int, status: str,
            failure: Optional[dict] = None) -> int:
    result["status"] = status
    result["exit_code"] = rc
    result["finished_at"] = _utcnow()
    if failure is not None:
        # The summary is what an alert and a page quote; whoever produced it,
        # it is stored bounded (see DETAIL_HEAD_CHARS).
        summary = failure.get("summary")
        if isinstance(summary, str):
            failure = {**failure, "summary": _bounded_detail(summary)}
        result["failure"] = failure
    else:
        result.pop("failure", None)
    if json_path:
        _atomic_json(json_path, result)
    return rc


def _finish_unmeasured(result: dict, json_path: Optional[str],
                       exc: ReleaseMeasurementUnknown) -> int:
    evidence = exc.as_dict()
    result.setdefault("unmeasured", []).append(evidence)
    print(f"[UNMEASURED] {exc.subject}: {exc.detail}; no release build or "
          "publish may follow from an unknown answer", file=sys.stderr)
    return _finish(result, json_path, RC_NEEDS_HUMAN, "unmeasured", {
        "stage": exc.stage,
        "category": exc.category,
        "retryable": True,
        "subject": exc.subject,
        "summary": exc.detail,
    })


def transient_failure_category(text: str) -> Optional[str]:
    """Classify only errors for which waiting can change the answer."""
    low = (text or "").lower()
    for category, needles in _TRANSIENT_FAILURES:
        if any(needle in low for needle in needles):
            return category
    return None


def _build_failure_category(text: str) -> Optional[str]:
    """`transient_failure_category` for a BUILD or COMPOSE diagnostic.

    A read-back-only category is not a transient here (see
    `_READBACK_ONLY_CATEGORIES`): it reads as unclassified, which keeps the one
    defence-in-depth retry and `retryable: false`, exactly as before it existed.
    """
    category = transient_failure_category(text)
    return None if category in _READBACK_ONLY_CATEGORIES else category


def _one_line_error(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for marker in ("fatal:", "error:", "timed out"):
        hit = next((ln for ln in reversed(lines) if marker in ln.lower()), None)
        if hit:
            return hit[-1200:]
    return (lines[-1] if lines else "command failed without diagnostic output")[-1200:]


def compose_with_retries(bake: List[str], root: Path,
                         run: Optional[Callable] = None,
                         sleep: Optional[Callable[[float], None]] = None,
                         max_transient_attempts: int = COMPOSE_TRANSIENT_ATTEMPTS,
                         delays: Tuple[int, ...] = COMPOSE_RETRY_DELAYS,
                         extra_env: Optional[dict] = None,
                         ) -> Tuple[int, str, List[dict]]:
    """Compose with bounded, classified retries and return an audit trail.

    Unclassified failures get one retry, preserving the previous defence in
    depth without multiplying a deterministic failure. Transport and DNS
    signatures get the longer bounded retry-count policy. The
    exact same command is rerun, so already-pushed artefacts and BuildKit cache
    are reused rather than rebuilt.
    """
    sleeper = sleep or time.sleep
    history: List[dict] = []
    attempt = 0
    while True:
        attempt += 1
        started = _utcnow()
        if run is None:
            rc, _, err = _sh(
                bake, cwd=root, stream=True,
                env={**BUILD_ENV, **(extra_env or {})},
                stall_grace=BUILD_STALL_GRACE_S,
            )
        else:
            rc, _, err = run(
                bake, cwd=root, stream=True,
                env={**BUILD_ENV, **(extra_env or {})},
                stall_grace=BUILD_STALL_GRACE_S,
            )
        category = _build_failure_category(err)
        history.append({
            "attempt": attempt,
            "started_at": started,
            "finished_at": _utcnow(),
            "exit_code": rc,
            "supervision_outcome": (
                "stalled" if rc == watchdog.RC_STALLED else "natural"),
            "total_runtime_ceiling_seconds": None,
            "category": category,
            "summary": None if rc == 0 else _one_line_error(err),
        })
        if rc == 0:
            return rc, err, history

        # A BuildKit progress stall is worth one clean restart, not five; the
        # extended window is exclusively for failures whose diagnostic names a
        # network condition. Retry count stays bounded; one healthy attempt's
        # runtime does not.
        limit = (max_transient_attempts
                 if category in _EXTENDED_RETRY_CATEGORIES else 2)
        if attempt >= limit:
            return rc, err, history
        delay = delays[min(attempt - 1, len(delays) - 1)] if delays else 0
        history[-1]["retry_in_seconds"] = delay
        why = category or "unclassified defence-in-depth retry"
        print(f"  compose attempt {attempt}/{limit} failed ({why}); "
              f"retrying in {delay}s with built artefacts and cache intact",
              flush=True)
        sleeper(delay)


#: How many tool artefacts build at once.
#:
#: The cap is about MEMORY, not cores. Each build runs its own `make -j`, so two
#: concurrent C++ links can want far more RAM than two cores' worth — and an
#: unbounded yosys took down two machines on 2026-08-19. Two is the smallest
#: number that delivers what sequential building could not: yosys and OpenROAD
#: overlapping instead of costing the sum of both.
#:
#: `GK_BUILD_PARALLEL` raises it for a host that can carry more. A value that is
#: not a positive integer is REFUSED rather than silently treated as the default,
#: because a typo that quietly serialises the build is invisible — it just looks
#: slow, which is the state this changed.
BUILD_PARALLEL_DEFAULT = 2


def _build_parallelism(pending: int) -> int:
    raw = os.environ.get("GK_BUILD_PARALLEL")
    if raw is None or raw == "":
        want = BUILD_PARALLEL_DEFAULT
    else:
        try:
            want = int(raw)
        except (TypeError, ValueError):
            raise SystemExit(
                f"GK_BUILD_PARALLEL={raw!r} is not an integer. Refusing rather "
                f"than falling back to {BUILD_PARALLEL_DEFAULT}: a typo that "
                f"quietly serialises every build only looks slow.")
        if want < 1:
            raise SystemExit(
                f"GK_BUILD_PARALLEL={raw!r} must be at least 1.")
    return max(1, min(want, pending))


def _sh(cmd, cwd=None, timeout=None, stream=False, env=None,
        stall_grace=None, progress=False, line_prefix=""):
    """Run a command. `stream=True` lets its output through to OUR stdout.

    Capturing is right for the short API calls, and wrong for the two builds:
    with `capture_output=True` nothing reaches the log until the process exits,
    so `--progress plain` — added precisely to make a build watchable — changed
    nothing. A compose wedged twice with no CPU, no disk and no network, and the
    only way to place it was reading dockerd's journal, because its own progress
    was sitting in a buffer that would only be flushed by the exit that never
    came.

    Streaming gives up the captured text for the failure message. The output is
    in the log instead, which is more than the tail we were printing.

    Long work uses a progress lease. A progressing command can run indefinitely;
    a flat output+CPU score becomes the distinct WATCHDOG_STALLED outcome. The
    ``timeout`` argument remains only for short request/probe calls.
    """
    full = None
    if env:
        full = dict(os.environ)
        full.update(env)
    try:
        if stream:
            # Tee the merged stream to the live log AND keep a bounded tail.
            # The tail is what lets the retry policy distinguish the DNS error
            # observed on 2026-08-17 from a deterministic Dockerfile failure.
            # `subprocess.run` with inherited descriptors could stream, or it
            # could capture, but not both; returning a placeholder erased the
            # only evidence the caller needed to decide whether waiting helps.
            sys.stdout.flush()
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=full, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            tail: deque[bytes] = deque()
            tail_size = 0
            progress_bytes = 0
            nonlocal_buf = [""]          # the partial line `line_prefix` holds

            def pump() -> None:
                nonlocal tail_size, progress_bytes
                assert proc.stdout is not None
                while True:
                    chunk = os.read(proc.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    progress_bytes += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
                    if line_prefix:
                        # CONCURRENT BUILDS SHARE ONE LOG. Chunks are not line
                        # aligned, so prefixing a chunk would cut lines in half
                        # and two tools would produce something neither of them
                        # said. Hold the partial line until its newline arrives.
                        nonlocal_buf[0] += text
                        while "\n" in nonlocal_buf[0]:
                            line, nonlocal_buf[0] = nonlocal_buf[0].split("\n", 1)
                            sys.stdout.write(f"{line_prefix}{line}\n")
                    else:
                        sys.stdout.write(text)
                    sys.stdout.flush()
                    tail.append(chunk)
                    tail_size += len(chunk)
                    while tail and tail_size > STREAM_TAIL_CHARS:
                        tail_size -= len(tail.popleft())

            reader = threading.Thread(target=pump, name="build-log-tee",
                                      daemon=True)
            reader.start()
            if stall_grace is not None:
                cpu = watchdog.HostProcessTreeCpuProbe(proc.pid)
                meter = watchdog.ProgressMeter(
                    size_fn=lambda: progress_bytes, cpu_fn=cpu.sample,
                )
                outcome, natural_rc = watchdog.supervise(
                    proc, meter.sample, cpu.terminate,
                    poll_s=BUILD_POLL_S, stall_grace_s=stall_grace,
                )
                rc = (watchdog.RC_STALLED if outcome == "stalled"
                      else int(natural_rc if natural_rc is not None else 0))
            else:
                rc = proc.wait() if timeout is None else proc.wait(timeout=timeout)
            reader.join(timeout=10)
            captured = b"".join(tail).decode("utf-8", errors="replace")
            if stall_grace is not None and outcome == "stalled":
                captured += (
                    f"\nWATCHDOG_STALLED: no forward progress (output or "
                    f"process-tree CPU) for > {stall_grace:g}s; killed as "
                    "stalled, not judged by total runtime."
                )
            return rc, "", captured
        if progress:
            run = watchdog.run_supervised(
                cmd, cwd=cwd, env=full, stall_grace_s=(
                    stall_grace if stall_grace is not None
                    else watchdog.DEFAULT_STALL_GRACE_S),
                poll_s=BUILD_POLL_S, process_tree_cpu_progress=True,
            )
            return run.rc, run.out, run.err
        kwargs = {
            "cwd": cwd, "capture_output": True, "text": True, "env": full,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        p = subprocess.run(cmd, **kwargs)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", (
            f"OPERATIONAL_TIMEOUT after {timeout}s: a short request/probe did "
            "not answer. Long build, compaction, and push work must use a "
            "progress lease instead of this path."
        )
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def _gh_tip(repo: str, branch: str) -> str:
    rc, out, _ = _sh(["gh", "api", f"repos/vibeic/{repo}/commits/{branch}",
                      "--jq", ".sha"], timeout=120)
    return out.strip() if rc == 0 else ""


def ref_arg_names(eda_root: Path) -> Dict[str, str]:
    """fork repo -> the `ARG <NAME>_REF` that pins it, without the `_REF` suffix.

    Reads the SAME pairing `pinned_refs` reads (vibeic-eda#104 finding 2). This
    used to mangle the repo name into an ARG name and look it up, which is a
    different rule from the one the pin set is built with — so the two could, and
    did, return different sets. Three asap7 forks resolved here to `None`, and
    `refs_now`/`refs_all` below drop anything `args_of` cannot name, which is how
    a fork could be pinned in a Dockerfile and still be missing from every
    artefact tag and every release record computed from it.
    """
    return {repo: site["arg"][:-len("_REF")]
            for repo, site in pin_sites(eda_root).items()}


def bake_targets(eda_root: Path) -> Dict[str, List[str]]:
    """bake target -> the REF variable names its TAG is composed from, in order.

    Read rather than hard-coded. `sat-solvers` is tagged
    `${short(KISSAT_REF)}-${short(CADICAL_REF)}` and `lvs` is
    `${short(MAGIC_REF)}-${short(NETGEN_REF)}`; a future pairing would otherwise
    have to be remembered here, and would not be.
    """
    hcl = (eda_root / "docker-bake.hcl").read_text(errors="replace")
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r'target\s+"([A-Za-z0-9_.-]+)"\s*\{(.*?)\n\}',
                         hcl, re.S):
        name, body = m.group(1), m.group(2)
        tags = re.search(r"tags\s*=\s*(.+)", body)
        if not tags:
            continue
        line = tags.group(1)
        refs = re.findall(r"short\(([A-Z0-9_]+_REF)\)", line)
        if not refs:                      # tool_tags("x", X_REF) helper form
            refs = re.findall(r"tool_tags\(\s*\"[^\"]+\"\s*,\s*([A-Z0-9_]+_REF)",
                              line)
        if refs:
            out[name] = refs
    return out


def _is_pure_mirror(repo: str) -> bool:
    """Does this fork carry NO commits of ours anywhere, on any branch?

    `branch_is_ours` asks about ONE branch. That is the right question for the
    case it guards -- vibeic-eda#23/#25, where slang, xschem, Xyce and sv-elab
    were pinned to the commits the image ships and the next release wanted to
    move all four to `master`, a four-tool version bump smuggled under "build 6
    absent artefacts".

    But for a fork that carries nothing ANYWHERE, that refusal is unconditional:
    the pin can never advance, so the fleet can never converge. Measured
    2026-08-06: OpenROAD-flow-scripts and slang are each 0 ahead of upstream and
    0 behind it -- exactly level -- while their pins trailed by 18 commits, and
    `release 18` was closed by hand every time. For such a fork the pin's only
    meaning is "which upstream commit do we build", and advancing it IS tracking
    upstream.

    Measured from the LOCAL CLONE, by ancestry, not from a name or a config flag:
    a fork becomes non-mirror the moment one commit of ours lands, and nobody has
    to remember to edit a list. Refuses to answer True when it cannot measure --
    an unreadable clone must not turn into "advance it freely".
    """
    clone = Path(os.environ.get("GK_FORKS_DIR")
                 or os.path.expanduser("~/vibe-ic-forks")) / repo
    if not (clone / ".git").is_dir():
        return False
    def _g(*a):
        try:
            r = subprocess.run(_release_git_cmd(clone, *a),
                               capture_output=True, text=True, timeout=120)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:                                            # noqa: BLE001
            return None
    up = None
    for cand in ("upstream/master", "upstream/main"):
        if _g("rev-parse", "--verify", "--quiet", cand):
            up = cand
            break
    if not up:
        return False
    ours = None
    for cand in ("origin/master", "origin/main"):
        if _g("rev-parse", "--verify", "--quiet", cand):
            ours = cand
            break
    if not ours:
        return False
    n = _g("rev-list", "--count", f"{up}..{ours}")
    if n is None or not n.isdigit():
        return False
    return int(n) == 0


def rewrite_pin(eda_root: Path, arg: str, new: str) -> List[str]:
    """Write `new` at every site that states this pin. Returns the paths changed.

    Only the 40-hex is replaced; the rest of the line survives untouched. That
    matters: these lines carry trailing comments recording what the branch holds,
    and an earlier bumper rewrote whole lines with `sed`, eating the comments —
    after which a downstream parser that read the branch name FROM the comment
    lost two of fourteen tools and reported twelve without saying so.
    """
    changed: List[str] = []
    files = list((eda_root / "tools").glob("*/Dockerfile"))
    files += [eda_root / "Dockerfile", eda_root / "docker-bake.hcl"]
    pat_arg = re.compile(rf"(^ARG\s+{re.escape(arg)}_REF=)[0-9a-f]{{40}}", re.M)
    pat_var = re.compile(
        rf'(variable\s+"{re.escape(arg)}_REF"\s*\{{\s*default\s*=\s*")[0-9a-f]{{40}}')
    for f in files:
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        new_text = pat_arg.sub(rf"\g<1>{new}", text)
        new_text = pat_var.sub(rf"\g<1>{new}", new_text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
            changed.append(str(f.relative_to(eda_root)))
    return changed


def retag_images(eda_root: Path, refs_now: Dict[str, str],
                 targets: Dict[str, List[str]]) -> List[str]:
    """Recompose every `ARG IMG_<TOOL>=...:<tag>` from the current pins."""
    root_df = eda_root / "Dockerfile"
    text = root_df.read_text(errors="replace")
    touched: List[str] = []
    for target, ref_vars in targets.items():
        full = artefact_tag(eda_root, target, ref_vars, refs_now)
        if full is None:
            continue
        tag = full.split(":", 1)[1]
        argname = "IMG_" + target.upper().replace("-", "_")
        pat = re.compile(
            rf"(^ARG\s+{argname}=\S*/eda-tool-{re.escape(target)}:)\S+", re.M)
        new_text = pat.sub(rf"\g<1>{tag}", text)
        if new_text != text:
            touched.append(f"{argname} -> :{tag}")
            text = new_text
    if touched:
        root_df.write_text(text, encoding="utf-8")
    return touched


# The one spelling of the recipe namespace inside the fingerprint's input set.
# Producer (`release_record_value`) and consumer (`check_release_recorded`) both
# read it from here, so the two cannot drift apart on a bare string literal.
RECIPE_KEY_PREFIX = "recipe:"


def pins_fingerprint(pins: Dict[str, str]) -> str:
    """A stable digest of the whole pin set — what a released image was built from."""
    blob = ";".join(f"{k}={v}" for k, v in sorted(pins.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def released_record(eda_root: Path) -> dict:
    f = eda_root / "RELEASED.json"
    if not f.is_file():
        return {}
    try:
        value = _strict_json_loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseMeasurementUnknown(
            stage="release_record",
            category="release_record_malformed",
            subject=str(f),
            detail=f"authoritative RELEASED.json is not strict JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise ReleaseMeasurementUnknown(
            stage="release_record",
            category="release_record_malformed",
            subject=str(f),
            detail="authoritative RELEASED.json must be a JSON object",
        )
    return value


def release_record_value(eda_root: Path, version: str,
                         targets: Dict[str, List[str]], *,
                         manifest_identity: Optional[str] = None) -> dict:
    """Construct the canonical record without changing the working tree."""
    digest_inputs = {
        **pinned_refs(eda_root),
        **{f"{RECIPE_KEY_PREFIX}{k}": recipe_hash(eda_root, k) for k in targets},
        f"{RECIPE_KEY_PREFIX}__compose__": compose_recipe_hash(eda_root)}
    rec = {
        "_comment": "What the last PUBLISHED image was built from. A pin set "
                    "not matching this has never been released, however current "
                    "the pins look. The fingerprint is measured AFTER every edit "
                    "this run made, so a later run can reproduce it from the "
                    "tree that shipped.",
        "version": version,
        "pins_fingerprint": pins_fingerprint(digest_inputs),
        "pins": {k: v for k, v in sorted(pinned_refs(eda_root).items())},
        # #77 — RECORD EVERYTHING THE FINGERPRINT WAS COMPUTED FROM. The digest
        # covers the pins AND the recipe hashes, but the record preserved only
        # the pins, so its own input set could not be reconstructed from it. A
        # later mismatch was therefore unattributable: a recipe-only edit moves
        # the fingerprint with every pin identical, and `check_release_recorded`
        # had nowhere to put that but UNREPRODUCIBLE — reporting a CORRECT
        # record as broken and holding the tick red until the next release.
        # That is #73's own complaint, arriving through the recipe side.
        # MEASURED: adding the image-version labels to `docker-bake.hcl` moved
        # `recipe:__compose__` 6a644b -> 94f87d with all 34 pins unchanged, and
        # 0.3.19's record — which reproduces EXACTLY from its release commit —
        # was called unreproducible.
        "recipes": {k: v for k, v in sorted(digest_inputs.items())
                    if k.startswith(RECIPE_KEY_PREFIX)},
    }
    if manifest_identity is not None:
        rec["manifest_identity"] = manifest_identity
    return rec


def write_released_record(eda_root: Path, version: str,
                          targets: Dict[str, List[str]], *,
                          manifest_identity: Optional[str] = None) -> dict:
    """THE ONE writer of `RELEASED.json`. Returns the record it wrote.

    vibeic-eda#51. This used to be an inline block inside the publish path, so
    the ledger was written only by a release that went THROUGH that path.
    0.2.53 was cut by hand in response to #45/#46 — VERSION advanced, the image
    was built and published, and the record still described 0.2.52. By the
    file's own contract ("a pin set not matching this has never been released")
    main's shipped pin set had never shipped, and the next tick would have
    published 0.2.54 byte-identical to 0.2.53.

    Extracted so a hand release can record itself through the SAME code — see
    `--record-release`. A second, hand-derived fingerprint would be the same
    defect wearing a different hat: the fingerprint MUST be computed the way the
    reader recomputes it or the record is unreproducible (measured on 0.2.45:
    the shipped tree hashed to e4e0a5f6 while the file recorded 94d85fda, and
    the next run started composing 0.2.46 with nothing changed).

    MEASURED AFTER every edit this run made, so a later run can reproduce it
    from the tree that shipped — `compose_recipe_hash` reads the root Dockerfile
    that `rewrite_pin` and `retag_images` have already edited.
    """
    rec = release_record_value(
        eda_root, version, targets, manifest_identity=manifest_identity)
    _atomic_json(eda_root / "RELEASED.json", rec)
    return rec


#: A changed line in a pin file that is a PIN and nothing else. Anything a
#: release run writes into these files matches one of these; anything that does
#: not is a human's edit and is refused rather than swept into a release commit.
_PIN_LINE_RES = (
    re.compile(
        r"^(\s*ARG\s+[A-Z0-9_]+_REF=)[0-9a-f]{40}"
        r"(\s*(?:#.*)?)$"),
    re.compile(
        r"^(\s*ARG\s+IMG_[A-Z0-9_]+=)\S+"
        r"(\s*(?:#.*)?)$"),
    re.compile(
        r'^(\s*variable\s+\"[A-Z0-9_]+_(?:REF|RECIPE)\"\s*\{\s*'
        r'default\s*=\s*\")[^\"]*(\"\s*\}\s*(?:#.*)?)$'),
)


def _pin_line_shape(line: str) -> Optional[str]:
    """Canonical shape of one mechanically writable pin line."""
    for pattern in _PIN_LINE_RES:
        match = pattern.fullmatch(line)
        if match:
            return match.group(1) + "<RELEASE-PIN>" + match.group(2)
    return None


def _git_error(blob: bytes) -> str:
    return blob.decode("utf-8", errors="replace").strip()


def _release_git_bytes(eda_root: Path, *args: str,
                       input_bytes: Optional[bytes] = None,
                       timeout: int = 60) -> Tuple[int, bytes, bytes]:
    """Run sanitised Git plumbing without decoding or content conversion."""
    try:
        result = subprocess.run(
            _release_git_cmd(eda_root, *args), input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 124, b"", str(exc).encode("utf-8", errors="replace")


def _head_tree_entries(
        eda_root: Path, revision: str = "HEAD",
        ) -> Tuple[Optional[Dict[str, Tuple[str, str, str]]], str]:
    """HEAD path -> (mode, object type, oid), without worktree filters."""
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "ls-tree", "-r", "-z", "--full-tree", revision),
        timeout=60)
    if rc != 0:
        return None, f"could not enumerate HEAD: {(err or out).strip()[:200]}"
    entries: Dict[str, Tuple[str, str, str]] = {}
    try:
        for record in (item for item in out.split("\0") if item):
            metadata, rel = record.split("\t", 1)
            mode, kind, oid = metadata.split(" ", 2)
            entries[rel] = (mode, kind, oid.lower())
    except ValueError as exc:
        return None, f"could not parse HEAD tree inventory: {exc}"
    return entries, ""


def _index_entries(
        eda_root: Path) -> Tuple[Optional[Dict[str, List[Tuple[str, str, int]]]], str]:
    """Raw index entries, retaining conflict stages instead of flattening them."""
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "ls-files", "--stage", "-z"), timeout=60)
    if rc != 0:
        return None, f"could not enumerate the release index: {(err or out).strip()[:200]}"
    entries: Dict[str, List[Tuple[str, str, int]]] = {}
    try:
        for record in (item for item in out.split("\0") if item):
            metadata, rel = record.split("\t", 1)
            mode, oid, stage = metadata.split(" ", 2)
            entries.setdefault(rel, []).append(
                (mode, oid.lower(), int(stage)))
    except ValueError as exc:
        return None, f"could not parse the release index: {exc}"
    return entries, ""


def _object_format(eda_root: Path) -> Tuple[Optional[str], str]:
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--show-object-format"), timeout=60)
    algorithm = out.strip().lower()
    if rc != 0 or algorithm not in {"sha1", "sha256"}:
        return None, ("could not determine Git object format: "
                      + (err or out).strip()[:160])
    return algorithm, ""


def _blob_oid(data: bytes, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _raw_worktree_blob(
        eda_root: Path, rel: str, algorithm: str,
        ) -> Tuple[Optional[Tuple[str, str, bytes]], str]:
    """Return (Git mode, raw oid, raw bytes); never run attributes/filters."""
    path = eda_root / rel
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"could not inspect working-tree {rel}: {exc}"
    try:
        if stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            mode = "100755" if info.st_mode & 0o111 else "100644"
        elif stat.S_ISLNK(info.st_mode):
            data = os.readlink(path).encode(sys.getfilesystemencoding(),
                                            errors="surrogateescape")
            mode = "120000"
        else:
            return None, f"working-tree {rel} is not a regular file or symlink"
    except OSError as exc:
        return None, f"could not read working-tree {rel}: {exc}"
    return (mode, _blob_oid(data, algorithm), data), ""


def _single_index_entry(
        entries: Dict[str, List[Tuple[str, str, int]]], rel: str,
        ) -> Optional[Tuple[str, str]]:
    values = entries.get(rel, [])
    if len(values) != 1 or values[0][2] != 0:
        return None
    return values[0][0], values[0][1]


def _raw_head_blob(eda_root: Path, oid: str) -> Tuple[Optional[bytes], str]:
    rc, out, err = _release_git_bytes(eda_root, "cat-file", "blob", oid)
    if rc != 0:
        return None, f"could not read HEAD blob {oid}: {_git_error(err or out)[:160]}"
    return out, ""


def _raw_pin_change_is_only_pins(
        *, head_mode: str, head_oid: str, head_bytes: bytes,
        index_entry: Optional[Tuple[str, str]],
        working: Tuple[str, str, bytes],
        ) -> Tuple[bool, str]:
    """Admit one already-captured raw pin replacement."""
    work_mode, work_oid, work_bytes = working
    if work_mode != head_mode:
        return False, "the release pin file mode or type changed"
    if index_entry not in {(head_mode, head_oid), (work_mode, work_oid)}:
        return False, "the release index carries a different staged pin edit"
    try:
        before = head_bytes.decode("utf-8").splitlines(keepends=True)
        after = work_bytes.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return False, "the release pin file is not UTF-8 text"
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    for delta in difflib.ndiff(before, after):
        if delta[:2] not in {"- ", "+ "}:
            continue
        line = delta[2:].rstrip("\r\n")
        shape = _pin_line_shape(line)
        if shape is None:
            return False, f"non-pin change: {line.strip()[:48]}"
        (removed if delta.startswith("- ") else added)[shape] += 1
    if not added and not removed:
        return False, "the release pin change no longer exists"
    if not added or not removed or added != removed:
        return False, "the release pin is not a paired replacement"
    return True, ""


def _diff_is_only_pins(eda_root: Path, rel: str) -> Tuple[bool, str]:
    """Does `rel`'s working-tree diff contain pin lines and nothing else?

    The question this answers is "is every change here one THIS PROGRAM makes",
    not "did this program make them" — a stricter reading would need to diff
    against a snapshot, and the looser one is what the refusal is for: keeping a
    human's half-finished edit out of an automated release commit.
    """
    # Git's diff machinery still applies clean filters even with --no-textconv
    # and --no-ext-diff. Compare raw committed/worktree bytes ourselves, and
    # separately prove that replacing the real index entry loses no staged data.
    head, why = _head_tree_entries(eda_root)
    if head is None:
        return False, why
    indexes, why = _index_entries(eda_root)
    if indexes is None:
        return False, why
    algorithm, why = _object_format(eda_root)
    if algorithm is None:
        return False, why
    head_entry = head.get(rel)
    if head_entry is None or head_entry[1] != "blob":
        return False, "the release pin has no regular HEAD blob"
    working, why = _raw_worktree_blob(eda_root, rel, algorithm)
    if working is None:
        return False, why or "the release pin change no longer exists"
    head_mode, _kind, head_oid = head_entry
    index_entry = _single_index_entry(indexes, rel)
    if index_entry is None:
        return False, "the release index has a missing or conflicted pin entry"
    head_bytes, why = _raw_head_blob(eda_root, head_oid)
    if head_bytes is None:
        return False, why
    return _raw_pin_change_is_only_pins(
        head_mode=head_mode, head_oid=head_oid, head_bytes=head_bytes,
        index_entry=index_entry, working=working,
    )


def commit_release_record(eda_root: Path, version: str,
                          pin_files: Optional[List[str]] = None
                          ) -> Tuple[bool, str]:
    """Commit VERSION + RELEASED.json. vibeic-eda#71.

    THE PUBLISH HALF IS DURABLE AND THE RECORD HALF WAS NOT. `docker push` puts
    the image in a registry, where it stays; `write_released_record` writes a
    file in the WORKING TREE and this program never shells out to git — measured
    by reading it, `_sh(["git", ...])` appears nowhere. So a checkout, a landing
    sequence, or anyone tidying a dirty tree discards the record, and the
    registry and the repo then disagree about what the current release is.

    Observed: 0.2.57 was built, smoke-tested and pushed, and `VERSION` +
    `RELEASED.json` on main both still said 0.2.56 — with a fingerprint that no
    longer reproduced, so `check_release_recorded` read "this pin set has never
    been released" about a pin set that had.

    PUBLISHING AND PIN-RECORDING ARE ONE ACTION — the same shape as vibe-ic#754
    ("publishing and anchoring are one action"), one step earlier in the same
    path.

    This used to commit ONLY the two record files, on the reasoning that "a
    release run's tree also holds the pin edits it made; those are a separate
    decision that goes through review". Measured over 0.2.65 -> 0.2.84, that
    review never once changed anything: THIRTY consecutive follow-up commits,
    every one of them recording exactly the refs the image was built from,
    because by the time this function runs there IS no other correct value —
    `rewrite_pin` wrote those refs, the image was built from them, an 11-tool
    smoke test passed on it, and `docker push` put it in a registry. The
    decision is forty minutes old and irrevocable; what was left was the
    bookkeeping, and leaving it out meant a fresh clone rebuilt something other
    than what this very commit's RELEASED.json claims shipped.

    So the pin files THIS RUN MOVED are committed here too, by explicit path,
    from the list the program already keeps (`rewrite_pin` returns the files it
    changed). Never `-A`.

    The old docstring's fear is real and is KEPT, narrowed to what it is
    actually about: sweeping in an edit nobody reviewed. A pin file whose
    working-tree diff contains anything other than pin-shaped lines (a ref, a
    recipe digest, an IMG tag) is REFUSED and named, because that is someone
    else's uncommitted work and a release commit is not where it belongs.

    Attempts the fail-safe push after committing, but its boolean describes the
    commit only for compatibility; the caller independently reads origin back
    before any green terminal. Returns (committed, note) and never raises — an
    already-published immutable image must retain an explicit recovery record
    even when the Git half cannot complete.
    """
    # README.md carries this repo's OWN install pointers, rewritten by
    # `sync_image_version --set` immediately before this call. It is listed here so
    # the docs land in the same commit as the VERSION they describe; left out, the
    # sync would run every release and be discarded every release. Still explicit,
    # still never `-A`.
    files = ["VERSION", "RELEASED.json", "README.md"]
    present = [f for f in files if (eda_root / f).is_file()]
    if not present:
        return False, "neither VERSION nor RELEASED.json exists"

    # The pin files this run moved, each admitted only if its diff is nothing
    # but pin lines. `refused` is reported, never silently dropped: a pin left
    # uncommitted is exactly the state check_release_pins_committed exists to
    # catch, so the operator has to hear about it here rather than tomorrow.
    admitted, refused = [], []
    for rel in sorted(set(pin_files or [])):
        if not (eda_root / rel).is_file():
            refused.append(f"{rel} (missing after the release build)")
            continue
        ok, why = _diff_is_only_pins(eda_root, rel)
        (admitted if ok else refused).append(rel if ok else f"{rel} ({why})")
    if refused:
        # The record was computed from the working tree that built the image.
        # Publishing it without every pin file named by this run would make
        # origin claim the new pin while retaining the old one -- the exact
        # stale-GitHub-pin defect this commit path exists to close.  Preserve
        # the human edit and the locally written record, but publish neither as
        # a falsely complete release.
        return False, (
            f"record NOT committed — {len(refused)} release pin file(s) "
            "REFUSED, so the record would not reproduce on origin: "
            + ", ".join(refused)
        )
    present = sorted(set(present + admitted))

    committed, note = _commit_release_record_exact(
        eda_root, version, present, admitted)
    if not committed:
        return False, note
    _pushed, _why = push_release_record(eda_root)
    note += f"; {_why}"
    return True, note


def _commit_release_record_exact(
        eda_root: Path, version: str, present: List[str], admitted: List[str],
        ) -> Tuple[bool, str]:
    """Create the exact release tree while holding the checkout's index lock."""
    # Claim the standard lock before the final index measurement.  Otherwise a
    # human `git add` between admission and index replacement can either ride
    # the release tree or be destroyed by it.  Read-only Git plumbing continues
    # to work while this inode exists; all cooperating index writers refuse.
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--path-format=absolute", "--git-path", "index"),
        timeout=60)
    index_text = out.strip()
    if rc != 0 or not index_text or "\x00" in index_text:
        return False, ("could not resolve the release index path: "
                       f"{(err or out).strip()[:160]}")
    index_path = Path(index_text)
    if not index_path.is_absolute():
        index_path = eda_root / index_path
    lock_path = Path(str(index_path) + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return False, f"could not lock the release index {lock_path}: {exc}"

    temporary_index: Optional[Path] = None
    try:
        # Freeze the parent, then inspect HEAD, the real index, and raw worktree
        # bytes as three independent states. `git diff HEAD` collapses index and
        # worktree changes: a staged human edit followed by a worktree restoration
        # can look clean, and a later replacement destroys that staged-only edit.
        rc, out, err = _sh(_release_git_cmd(
            eda_root, "rev-parse", "--verify", "HEAD^{commit}"), timeout=60)
        parent = out.strip().lower()
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", parent):
            return False, ("could not resolve release commit parent: "
                           f"{(err or out).strip()[:160]}")
        head, why = _head_tree_entries(eda_root, parent)
        if head is None:
            return False, why
        indexes, why = _index_entries(eda_root)
        if indexes is None:
            return False, why
        algorithm, why = _object_format(eda_root)
        if algorithm is None:
            return False, why

        def head_signature(rel: str) -> Optional[Tuple[str, str]]:
            item = head.get(rel)
            return (item[0], item[2]) if item is not None else None

        def index_state(rel: str) -> Tuple[bool, Optional[Tuple[str, str]]]:
            values = indexes.get(rel, [])
            if not values:
                return True, None
            if len(values) == 1 and values[0][2] == 0:
                return True, (values[0][0], values[0][1])
            return False, None

        # A pathspec commit used to leave unrelated staged work alone. Plumbing
        # the exact index is safe only if that property is preserved.
        staged_paths = set()
        for rel in set(head) | set(indexes):
            valid, indexed = index_state(rel)
            if not valid or indexed != head_signature(rel):
                staged_paths.add(rel)
        unrelated_staged = staged_paths - set(present)
        if unrelated_staged:
            return False, (
                "record NOT committed — unrelated staged paths would be swept: "
                + ", ".join(sorted(unrelated_staged))
            )

        planned: Dict[str, Tuple[str, str, bytes]] = {}
        for rel in present:
            working, why = _raw_worktree_blob(eda_root, rel, algorithm)
            if working is None:
                return False, why or f"working-tree {rel} disappeared"
            mode, oid, raw = working
            if mode not in {"100644", "100755"}:
                return False, f"working-tree {rel} is not a regular release file"
            item = head.get(rel)
            if item is not None and item[1] != "blob":
                return False, f"HEAD:{rel} is not a regular release blob"
            valid, indexed = index_state(rel)
            if not valid:
                return False, (
                    f"record NOT committed — {rel} has conflicted index stages")
            before = head_signature(rel)
            after = (mode, oid)
            if indexed not in {before, after}:
                return False, (
                    f"record NOT committed — {rel} carries a different staged "
                    "edit; the release will not overwrite it"
                )
            if rel in admitted:
                if item is None:
                    return False, (
                        f"record NOT committed — HEAD:{rel} disappeared during "
                        "pin admission"
                    )
                head_bytes, why = _raw_head_blob(eda_root, item[2])
                if head_bytes is None:
                    return False, why
                pin_ok, pin_why = _raw_pin_change_is_only_pins(
                    head_mode=item[0], head_oid=item[2],
                    head_bytes=head_bytes, index_entry=indexed,
                    working=working,
                )
                if not pin_ok:
                    return False, (
                        f"record NOT committed — {rel} changed during pin "
                        f"admission ({pin_why})"
                    )
            planned[rel] = working

        if all((mode, oid) == head_signature(rel)
               for rel, (mode, oid, _raw) in planned.items()):
            return False, "record already matches HEAD — nothing to commit"

        # Populate a private index from the frozen parent. The real index stays
        # locked and untouched until the HEAD compare-and-swap succeeds.  In
        # particular, Git never refreshes a racy real-index entry through a
        # repository clean filter while an unrelated release path is staged.
        try:
            with tempfile.NamedTemporaryFile(
                    prefix=f".{index_path.name}.release-",
                    dir=index_path.parent, delete=True) as scratch:
                temporary_index = Path(scratch.name)
        except OSError as exc:
            return False, f"could not allocate the private release index: {exc}"
        rc, out, err = _sh(_release_git_cmd(
            eda_root, "read-tree", parent, index_file=temporary_index),
            timeout=60)
        if rc != 0:
            return False, ("could not initialise the private release index: "
                           f"{(err or out).strip()[:160]}")

        # Write raw blobs and private index entries only. `--no-filters` plus
        # `--cacheinfo` makes attributes incapable of executing a helper or of
        # changing the bytes which the release image consumed.
        for rel, (mode, expected_oid, raw) in planned.items():
            rc, oid_out, oid_err = _release_git_bytes(
                eda_root, "hash-object", "-w", "--no-filters", "--stdin",
                input_bytes=raw,
            )
            written_oid = _git_error(oid_out).lower()
            if rc != 0 or written_oid != expected_oid:
                return False, (f"could not stage raw release bytes for {rel}: "
                               f"{_git_error(oid_err or oid_out)[:160]}")
            rc, out, err = _sh(_release_git_cmd(
                eda_root, "update-index", "--add", "--cacheinfo",
                mode, expected_oid, rel, index_file=temporary_index), timeout=60)
            if rc != 0:
                return False, (f"could not stage exact release blob {rel}: "
                               f"{(err or out).strip()[:160]}")

        rc, out, err = _sh(_release_git_cmd(
            eda_root, "write-tree", index_file=temporary_index), timeout=60)
        tree = out.strip().lower()
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            return False, f"git write-tree failed: {(err or out).strip()[:160]}"
        msg = (f"release: record {version} as published\n"
               f"\n"
               f"Written by daily_release after the image was pushed. The "
               f"publish is durable (it is in a registry); this is the half "
               f"that was not (vibeic-eda#71).\n")
        if admitted:
            msg += ("\nThe pins this run moved are committed here too, so a fresh "
                    "clone rebuilds what this record says shipped:\n"
                    + "".join(f"  {f}\n" for f in admitted))
        rc, out, err = _sh(_release_git_cmd(
            eda_root, "commit-tree", tree, "-p", parent, "-m", msg),
            timeout=120)
        commit = out.strip().lower()
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            return False, f"git commit-tree failed: {(err or out).strip()[:160]}"
        rc, out, err = _sh(_release_git_cmd(
            eda_root, "update-ref", "-m", f"release: record {version}",
            "HEAD", commit, parent), timeout=60)
        if rc != 0:
            return False, ("release commit was constructed but HEAD CAS failed: "
                           f"{(err or out).strip()[:160]}")

        # The private index already names exactly the new commit tree. Install
        # it atomically while the standard lock still excludes Git writers;
        # never ask Git to re-read working bytes through attributes.
        try:
            os.replace(temporary_index, index_path)
            temporary_index = None
        except OSError as exc:
            # HEAD is already durably advanced. Calling that commit a failure
            # would invite a duplicate recovery; surface the local cleanup
            # problem without lying about the recorded release.
            note = (f"recorded {version} in a commit; WARNING: could not "
                    f"refresh the checkout index: {exc}")
            return True, note
        note = f"recorded {version} in a commit"
        if admitted:
            note += f" with {len(admitted)} pin file(s)"
        return True, note
    finally:
        if temporary_index is not None:
            try:
                temporary_index.unlink(missing_ok=True)
            except OSError:
                pass
        os.close(lock_fd)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


_GIT_ENV_UNSET = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_ATTR_SOURCE", "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF", "GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE",
    "GIT_DIFF_OPTS",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_SSL_NO_VERIFY", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_OPTIONAL_LOCKS", "GIT_QUARANTINE_PATH",
)


def _release_git_cmd(
        eda_root: Path, *args: str,
        index_file: Optional[Path] = None,
        ) -> List[str]:
    """Git invocation whose object/ref meaning is not inherited from ambient env."""
    command = ["/usr/bin/env"]
    for name in _GIT_ENV_UNSET:
        command.extend(["-u", name])
    command.extend([
        "GIT_NO_REPLACE_OBJECTS=1", "GIT_ATTR_NOSYSTEM=1",
        "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_SYSTEM=/dev/null",
        "GIT_CONFIG_GLOBAL=/dev/null", "GIT_EXEC_PATH=/usr/lib/git-core",
        "GIT_SSH=/usr/bin/ssh", "GIT_TERMINAL_PROMPT=0", "LC_ALL=C",
    ])
    if index_file is not None:
        command.append(f"GIT_INDEX_FILE={index_file}")
    command.extend([
        "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "credential.helper=",
        "-c", "credential.https://github.com.helper=!/usr/bin/gh auth git-credential",
        "-C", str(eda_root), *args,
    ])
    return command


def _fetch_remote_branch_commit(eda_root: Path, branch: str) \
        -> Tuple[Optional[str], str]:
    """Fetch one remote branch into a private ref and return its exact commit.

    ``git fetch origin main`` is not enough: with a missing/custom
    ``remote.origin.fetch`` it updates only FETCH_HEAD and leaves origin/main
    stale.  FETCH_HEAD itself is shared and can be overwritten concurrently.
    A unique explicit destination avoids both false answers.  Replace refs are
    disabled for every object read so the returned SHA means the same object on
    GitHub and locally.
    """
    if (not branch or branch.startswith("-") or ".." in branch
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)):
        return None, f"unsafe remote branch name: {branch!r}"
    fetch_ref = (f"refs/vibeic-release-probe/{os.getpid()}-"
                 f"{threading.get_ident()}-{time.time_ns()}")
    rc, _, err = _sh(_release_git_cmd(
        eda_root, "show-ref", "--verify", "--quiet", fetch_ref))
    if rc == 0:
        return None, f"private fetch ref collision: {fetch_ref}"
    if rc not in (1,):
        return None, f"could not check private fetch ref: {err.strip()[:120]}"

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "fetch", "--progress",
        "--no-write-fetch-head", "origin",
        f"+refs/heads/{branch}:{fetch_ref}",
    ), progress=True, stall_grace=PUSH_STALL_GRACE_S)
    if rc != 0:
        kind = "stalled" if rc == watchdog.RC_STALLED else "failed"
        return None, f"fetch {kind}: {(err or out).strip()[:400]}"

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--verify",
        f"{fetch_ref}^{{commit}}",
    ))
    commit = out.strip().lower()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        _sh(_release_git_cmd(eda_root, "update-ref", "-d", fetch_ref))
        return None, (f"fetched ref is not a commit: "
                      f"{(err or out).strip()[:160]}")

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "update-ref", "-d", fetch_ref, commit))
    if rc != 0:
        return None, (f"could not remove private fetch ref with CAS: "
                      f"{(err or out).strip()[:160]}")
    return commit, ""


def push_release_record(eda_root: Path, branch: str = "main") -> Tuple[bool, str]:
    """A release record nobody can read is not a record.

    MEASURED 2026-08-12. The 05:30 round published `vibeic-eda:0.2.86` to the
    registry at 06:19, committed the record plus the eight pin files it had
    moved -- and nothing pushed it. `grep -n "git push" run_0530.sh
    daily_0530.py daily_release.py` was EMPTY. The commit sat on the cron
    host's checkout:

        local  8a1f10c  release: record 0.2.86 as published
        origin 7891724  (one commit behind)

    Three things follow, and the first is what made it visible:

      * THE PUBLIC PAGE INVENTS GAPS THAT DO NOT EXIST. `discover_forks` reads
        each pin from GITHUB (`_gh_file`), not from this checkout, so it read
        the pre-release ngspice pin b2e9c4067 while the repo already said
        94d5ff8e1. Against the stale pin it reported "1 substantive commit of
        ours past the pin, not shipped"; against the real one,
        `rev-list 94d5ff8e..origin/master --not upstream/master` is 0. The
        owner asked why the page did not say zero. It was right to ask.
      * A FRESH CLONE REBUILDS SOMETHING OTHER THAN WHAT SHIPPED -- exactly the
        failure `commit_release_record` exists to prevent. That fix made the
        pins get COMMITTED; it did not make them get PUBLISHED, which is half
        of it.
      * The image version the page renders comes from GitHub's VERSION too, so
        it lags the same way.

    PUBLISHING AND RECORDING ARE ONE ACTION. By the time this runs the image is
    in a registry and the decision is irrevocable; leaving the record behind
    does not make anything safer, it only makes the repository disagree with
    the world. Same shape as vibe-ic#754 ("publishing and anchoring are one
    action"), one step further along the same path.

    FAIL-SAFE ABOUT OTHER PEOPLE'S WORK. It refuses unless `origin/<branch>` is
    an ancestor of HEAD, so it can never discard a commit that arrived while
    the image was building -- a real window, since the build takes tens of
    minutes. A refusal is reported, never silent: the caller prints it and the
    record stays committed for a human to land.
    """
    remote_commit, why = _fetch_remote_branch_commit(eda_root, branch)
    if remote_commit is None:
        return False, f"NOT PUSHED ({why})"

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--verify", "HEAD^{commit}"))
    local_commit = out.strip().lower()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", local_commit):
        return False, ("NOT PUSHED — could not resolve the exact local HEAD: "
                       f"{(err or out).strip()[:160]}")

    rc, _, _ = _sh(_release_git_cmd(
        eda_root, "merge-base", "--is-ancestor", remote_commit, local_commit))
    if rc != 0:
        return False, (f"NOT PUSHED — origin/{branch} is not an ancestor of HEAD; "
                       f"the remote moved while the image was building. The "
                       f"record is committed; land it by hand.")

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "push", "--progress",
        f"--force-with-lease=refs/heads/{branch}:{remote_commit}",
        "origin", f"{local_commit}:refs/heads/{branch}"), progress=True,
                       stall_grace=PUSH_STALL_GRACE_S)
    if rc != 0:
        kind = "stalled; state UNKNOWN" if rc == watchdog.RC_STALLED else "failed"
        return False, f"NOT PUSHED (push {kind}: {(err or out).strip()[:200]})"

    published_commit, why = _fetch_remote_branch_commit(eda_root, branch)
    if published_commit is None:
        return False, f"NOT PUSHED (push returned zero but readback {why})"
    if published_commit != local_commit:
        return False, (f"NOT PUSHED (push returned zero but origin/{branch} "
                       f"read back as {published_commit}, not {local_commit})")
    return True, "pushed"


_LOCAL_ABSENCE = re.compile(
    r"(?:no such image|no such object|image\s+\S+\s+not found)", re.IGNORECASE,
)
_REMOTE_ABSENCE = re.compile(
    r"(?:no such manifest|manifest unknown|name unknown|"
    r"manifest\b[^\n]*\bnot found|not found\b[^\n]*\bmanifest)",
    re.IGNORECASE,
)


def _probe_local_image(ref: str) -> PresenceMeasurement:
    """Measure local image presence without converting daemon failure to no."""
    rc, out, err = _sh(["docker", "image", "inspect", ref], timeout=60)
    detail = (err or out).strip()
    if rc == 0:
        return PresenceMeasurement(Presence.PRESENT, ref, "local", detail)
    if _LOCAL_ABSENCE.search(detail):
        return PresenceMeasurement(Presence.ABSENT, ref, "local", detail)
    return PresenceMeasurement(Presence.UNKNOWN, ref, "local", detail or (
        f"docker image inspect exited {rc} without an explicit not-found answer"
    ))


def _probe_remote_manifest(ref: str, *,
                           sleep: Optional[Callable[[float], None]] = None,
                           ) -> PresenceMeasurement:
    """Measure a registry tag; only an explicit manifest absence is ABSENT.

    Transient transport failures -- and the docker CLI's "unsupported manifest
    format" fallback, which masks a failed first fetch (see
    ``registry_manifest_unstable``) -- are retried before the final UNKNOWN
    verdict.  The retry does not change the meaning of an answer: only an
    explicit not-found reply becomes ABSENT only if no previous reply showed
    existence. The unsupported-format fallback prints a fetched manifest; any
    later not-found contradicts that evidence and remains UNKNOWN. Only rc 0
    is PRESENT. Classification reads the full reply; callers bound storage.
    """
    sleeper = sleep or time.sleep
    existence_seen = False
    for attempt in range(1, MANIFEST_UNSTABLE_ATTEMPTS + 1):
        rc, out, err = _sh(["docker", "manifest", "inspect", ref], timeout=180)
        detail = (err or out).strip()
        if rc == 0:
            identity = None
            try:
                # The tag name is not part of a Docker manifest.  Canonicalising the
                # returned object therefore gives the same identity for two tags
                # pointing at the same image, without trusting a local tag/cache.
                manifest = _strict_json_loads(out)
                canonical = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                identity = hashlib.sha256(canonical).hexdigest()
            except (TypeError, ValueError):
                # Presence is still known, but callers asking WHICH manifest must
                # retain this as an unmeasured identity rather than guessing.
                pass
            return PresenceMeasurement(
                Presence.PRESENT, ref, "registry", detail, identity,
            )
        # Inspect the existence marker independently of category priority:
        # network text and even manifest bytes containing "manifest unknown"
        # must not erase the fact that the CLI fetched a manifest.
        unstable = "unsupported manifest format" in detail.lower()
        existence_seen = existence_seen or unstable
        if not unstable and _REMOTE_ABSENCE.search(detail):
            if existence_seen:
                return PresenceMeasurement(
                    Presence.UNKNOWN, ref, "registry",
                    "registry contradicted prior manifest existence evidence: "
                    + detail,
                )
            return PresenceMeasurement(Presence.ABSENT, ref, "registry", detail)

        category = transient_failure_category(detail)
        attempts = (MANIFEST_UNSTABLE_ATTEMPTS if existence_seen
                    else MANIFEST_TRANSIENT_ATTEMPTS)
        delays = (MANIFEST_UNSTABLE_RETRY_DELAYS if existence_seen
                  else MANIFEST_RETRY_DELAYS)
        if (category not in _EXTENDED_RETRY_CATEGORIES
                or attempt >= attempts):
            return PresenceMeasurement(
                Presence.UNKNOWN, ref, "registry", detail or (
                    "docker manifest inspect exited "
                    f"{rc} without an explicit not-found answer"
                ))
        delay = delays[min(attempt - 1, len(delays) - 1)]
        print(f"  registry manifest probe {attempt}/{attempts} "
              f"for {ref} failed ({category}); retrying in {delay}s",
              flush=True)
        sleeper(delay)

    raise AssertionError("manifest retry loop exhausted without a terminal answer")


RELEASE_IMAGE = "ghcr.io/vibeic/vibeic-eda"


def verify_remote_immutable_publication(
        version: Optional[str], *, image: str = RELEASE_IMAGE,
        probe: Optional[Callable[[str], PresenceMeasurement]] = None,
        ) -> Tuple[bool, dict]:
    """Read an immutable version tag back and freeze its manifest identity."""
    if not version:
        return False, {
            "category": "version_missing",
            "summary": "no immutable release version was supplied",
            "retryable": False,
        }

    ref = f"{image}:{version}"
    measured = (probe or _probe_remote_manifest)(ref)
    evidence = {
        "ref": measured.subject,
        "state": measured.state.value,
        "source": measured.source,
        "identity": measured.identity,
        "detail": _bounded_detail(measured.detail),
    }
    if measured.state is Presence.UNKNOWN:
        raise ReleaseMeasurementUnknown(
            stage="immutable_publication",
            category="immutable_manifest_unmeasurable",
            subject=measured.subject,
            detail=f"{measured.source} manifest unmeasured: {measured.detail}",
        )
    if measured.state is Presence.ABSENT:
        evidence.update({
            "verified": False,
            "category": "immutable_manifest_missing",
            "summary": f"{measured.subject} is absent after its push",
            "retryable": True,
        })
        return False, evidence
    if not measured.identity:
        raise ReleaseMeasurementUnknown(
            stage="immutable_publication",
            category="immutable_manifest_identity_unmeasurable",
            subject=measured.subject,
            detail=("registry confirmed the immutable tag exists but returned "
                    "no parseable manifest identity"),
        )
    evidence["verified"] = True
    return True, evidence


def verify_remote_release_publication(
        version: Optional[str], *, image: str = RELEASE_IMAGE,
        probe: Optional[Callable[[str], PresenceMeasurement]] = None,
        ) -> Tuple[bool, dict]:
    """Verify an immutable release tag and ``:latest`` from the registry.

    This is deliberately injectable: the production default performs two
    read-only registry probes, while tests supply exact measurements and never
    contact Docker or a registry.  Merely finding both tags is insufficient --
    a failed latest push leaves an old, perfectly PRESENT latest behind.
    """
    if not version:
        return False, {
            "category": "recorded_version_missing",
            "summary": "RELEASED.json has no recorded version to verify",
            "retryable": False,
        }

    ask = probe or _probe_remote_manifest
    version_ref = f"{image}:{version}"
    latest_ref = f"{image}:latest"
    measured = {
        "version": ask(version_ref),
        "latest": ask(latest_ref),
    }
    evidence = {
        key: {
            "ref": item.subject,
            "state": item.state.value,
            "source": item.source,
            "identity": item.identity,
            "detail": _bounded_detail(item.detail),
        }
        for key, item in measured.items()
    }

    for key, item in measured.items():
        if item.state is Presence.UNKNOWN:
            raise ReleaseMeasurementUnknown(
                stage="noop_publication",
                category=f"{key}_manifest_unmeasurable",
                subject=item.subject,
                detail=(f"{item.source} manifest unmeasured: {item.detail}"),
            )
        if item.state is Presence.ABSENT:
            return False, {
                "category": ("recorded_version_manifest_missing"
                             if key == "version" else "latest_manifest_missing"),
                "summary": f"{item.subject} is absent from the registry",
                "retryable": True,
                "manifests": evidence,
            }

    for key, item in measured.items():
        if not item.identity:
            raise ReleaseMeasurementUnknown(
                stage="noop_publication",
                category=f"{key}_manifest_identity_unmeasurable",
                subject=item.subject,
                detail=("registry confirmed the tag exists but returned no "
                        "parseable manifest identity"),
            )

    if measured["version"].identity != measured["latest"].identity:
        return False, {
            "category": "latest_manifest_mismatch",
            "summary": (f"{latest_ref} does not identify the recorded "
                        f"{version_ref} manifest"),
            "retryable": True,
            "manifests": evidence,
        }
    return True, {"verified": True, "manifests": evidence}


def repair_latest_publication(
        version: Optional[str], *, image: str = RELEASE_IMAGE,
        probe: Optional[Callable[[str], PresenceMeasurement]] = None,
        publish: Optional[Callable[[str, str], Tuple[int, str, str]]] = None,
        authorize: Optional[Callable[[str], Tuple[bool, dict]]] = None,
        expected_identity: Optional[str] = None,
        repair: bool = True,
        ) -> Tuple[bool, dict]:
    """Make ``:latest`` name an already-published immutable version.

    The immutable version tag is the source of truth.  We mutate only after a
    read-only probe proves that tag exists and proves latest is absent or names
    a different manifest.  The registry-side copy avoids depending on a local
    daemon cache surviving from the release that originally pushed the image.
    A second independent probe is required before success is reported.
    """
    ok, initial = verify_remote_release_publication(
        version, image=image, probe=probe)
    evidence = dict(initial)
    evidence["initial"] = initial
    evidence["repair_attempted"] = False
    frozen_identity = (initial.get("manifests", {})
                       .get("version", {}).get("identity"))
    evidence["frozen_identity"] = frozen_identity
    if expected_identity is not None:
        if (not isinstance(expected_identity, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_identity)):
            evidence.update({
                "verified": False,
                "repairable": False,
                "category": "expected_manifest_identity_invalid",
                "summary": "latest repair has no valid recorded manifest identity",
            })
            return False, evidence
        if frozen_identity != expected_identity:
            evidence.update({
                "verified": False,
                "repairable": False,
                "category": "immutable_manifest_changed",
                "summary": (f"{image}:{version} no longer has the identity "
                            "which authorised latest repair"),
            })
            return False, evidence
    if ok:
        evidence["verified"] = True
        return True, evidence

    category = initial.get("category")
    if category not in {"latest_manifest_missing", "latest_manifest_mismatch"}:
        evidence["verified"] = False
        evidence["repairable"] = False
        return False, evidence
    if not repair:
        evidence["repair_deferred"] = True
        return False, evidence

    version_ref = f"{image}:{version}"
    latest_ref = f"{image}:latest"

    # The first manifest probe is intentionally not mutation authority.  A
    # newer release can publish between that read and the mutable-tag write.
    # The host-wide lock closes that window for every supported invocation
    # on the release host; this second, independent source-of-truth check also
    # refuses a stale checkout if origin advanced before the lock was acquired.
    if authorize is not None:
        authorised, authority = authorize(str(version))
        evidence["authority"] = authority
        if not authorised:
            evidence["verified"] = False
            evidence["repairable"] = False
            evidence["category"] = "release_authority_changed"
            evidence["summary"] = (
                authority.get("summary")
                or "the authoritative release changed before latest repair"
            )
            return False, evidence

    # Freeze is a value, not merely an ordering point.  Re-read the immutable
    # name after Git authorization and require the same identity immediately
    # before the mutable write.  A replaced version tag must not be copied just
    # because both names can later be made to agree on the replacement.
    immutable_ok, immutable = verify_remote_immutable_publication(
        version, image=image, probe=probe)
    evidence["pre_repair_immutable"] = immutable
    required_identity = expected_identity or frozen_identity
    if (not immutable_ok
            or immutable.get("identity") != required_identity):
        evidence.update({
            "verified": False,
            "repairable": False,
            "category": "immutable_manifest_changed",
            "summary": (f"{version_ref} changed after its authoritative "
                        "readback; :latest was not moved"),
        })
        return False, evidence

    def registry_copy(source: str, target: str) -> Tuple[int, str, str]:
        """Republish `source`'s bytes under `target`, PRESERVING THE MEDIA TYPE.

        `docker buildx imagetools create --tag target source` was the obvious
        way to do this registry-side, and it cannot work here: given a single
        image manifest it always writes an OCI **index** wrapping it. The
        immutable version tag is a plain `application/vnd.oci.image.manifest.v1+json`,
        an index digest never equals the manifest digest it wraps, and
        `verify_remote_release_publication` compares identities -- so the
        repair republished the right BYTES under the right name and then
        reported `latest_manifest_mismatch` about its own work, on that round
        and on every round after it.

        Measured 2026-09-07 while recovering 0.3.50 (vibeic-eda#194):

            :latest   index    4074074cb0d8dd99...  -> manifests: [5e2b472f8c6be44e...]
            :0.3.50   manifest 5e2b472f8c6be44e...
            repair -> False, category latest_manifest_mismatch, repair_attempted true

        pull + tag + push is what the RELEASE path itself does when it moves
        :latest after a build, so both paths now produce one shape and the
        verifier can be believed. The explicit pull is what the registry-side
        copy was reaching for: it does not depend on a daemon cache surviving
        from the release that first pushed the image.
        """
        rc, out, err = _sh(["docker", "pull", source],
                           progress=True, stall_grace=PUSH_STALL_GRACE_S)
        if rc != 0:
            return rc, out, err
        rc, out2, err2 = _sh(["docker", "tag", source, target])
        if rc != 0:
            return rc, out + out2, err + err2
        rc, out3, err3 = _sh(["docker", "push", target],
                             progress=True, stall_grace=PUSH_STALL_GRACE_S)
        return rc, out + out2 + out3, err + err2 + err3

    evidence["repair_attempted"] = True
    rc, out, err = (publish or registry_copy)(version_ref, latest_ref)
    evidence["repair"] = {
        "source": version_ref,
        "target": latest_ref,
        "exit_code": rc,
    }
    if rc != 0:
        evidence["repair"]["error"] = _one_line_error(err or out)
        evidence["verified"] = False
        return False, evidence

    final_ok, final = verify_remote_release_publication(
        version, image=image, probe=probe)
    evidence["final"] = final
    evidence.update(final)
    final_identity = (final.get("manifests", {})
                      .get("version", {}).get("identity"))
    if final_ok and final_identity != required_identity:
        final_ok = False
        evidence.update({
            "category": "immutable_manifest_changed",
            "summary": (f"{version_ref} changed while :latest was being "
                        "repaired; the repair is not authoritative"),
        })
    evidence["verified"] = final_ok
    return final_ok, evidence


def _probe_release_record_publication(
        eda_root: Path, branch: str = "main") -> PresenceMeasurement:
    """Is the commit carrying this checkout's RELEASED.json on origin?

    The probe fetches and inspects git objects only; it never pushes.  Testing
    ancestry of the commit that last changed RELEASED.json (rather than HEAD)
    avoids publishing unrelated local commits merely because a later noop wants
    to repair an earlier record-push failure.
    """
    subject = f"origin/{branch}:RELEASED.json"
    remote_commit, why = _fetch_remote_branch_commit(eda_root, branch)
    if remote_commit is None:
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            why,
        )

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--verify", "HEAD^{commit}"))
    local_commit = out.strip().lower()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", local_commit):
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not resolve the local HEAD commit: "
            f"{(err or out).strip()[:400]}",
        )

    # Compare bytes from the captured commit, not `git diff`: assume-unchanged,
    # skip-worktree, or a foreign index can hide a modified working-tree record.
    for rel in ("RELEASED.json", "VERSION"):
        rc, committed, err = _sh(_release_git_cmd(
            eda_root, "show", f"{local_commit}:{rel}"))
        if rc != 0:
            return PresenceMeasurement(
                Presence.UNKNOWN, subject, "git",
                f"could not read {rel} from local HEAD: "
                f"{(err or committed).strip()[:400]}",
            )
        try:
            working = (eda_root / rel).read_text(encoding="utf-8")
        except OSError as exc:
            return PresenceMeasurement(
                Presence.UNKNOWN, subject, "git",
                f"could not read working-tree {rel}: {exc}",
            )
        if committed != working:
            return PresenceMeasurement(
                Presence.ABSENT, subject, "git",
                f"the working-tree {rel} differs from local HEAD",
            )

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "log", "-1", "--format=%H", local_commit, "--",
        "RELEASED.json"))
    record_commit = out.strip()
    if rc != 0:
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not resolve the local record commit: "
            f"{(err or out).strip()[:400]}",
        )
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", record_commit):
        return PresenceMeasurement(
            Presence.ABSENT, subject, "git",
            "no commit in HEAD records RELEASED.json",
        )

    # An ancestor test alone proves only that this record existed on origin at
    # some point.  A later remote commit can replace it, so compare the remote
    # branch's CURRENT record and VERSION with this checkout too.
    rc, remote_record, err = _sh(_release_git_cmd(
        eda_root, "show", f"{remote_commit}:RELEASED.json"))
    if rc != 0:
        low = (err or remote_record).lower()
        if "does not exist" in low or "exists on disk, but not in" in low:
            return PresenceMeasurement(
                Presence.ABSENT, subject, "git",
                f"origin/{branch} has no RELEASED.json",
            )
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not read origin/{branch}:RELEASED.json: "
            f"{(err or remote_record).strip()[:400]}",
        )
    try:
        local_record = _strict_json_loads(
            (eda_root / "RELEASED.json").read_text())
        remote_record_value = _strict_json_loads(remote_record)
    except (OSError, ValueError) as exc:
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not compare local and remote release records: {exc}",
        )
    if remote_record_value != local_record:
        return PresenceMeasurement(
            Presence.ABSENT, subject, "git",
            f"origin/{branch}'s current RELEASED.json differs from this checkout",
            record_commit,
        )

    rc, remote_version, err = _sh(_release_git_cmd(
        eda_root, "show", f"{remote_commit}:VERSION"))
    if rc != 0:
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not read origin/{branch}:VERSION: "
            f"{(err or remote_version).strip()[:400]}",
        )
    try:
        local_version = (eda_root / "VERSION").read_text().strip()
    except OSError as exc:
        return PresenceMeasurement(
            Presence.UNKNOWN, subject, "git",
            f"could not read local VERSION: {exc}",
        )
    if remote_version.strip() != local_version:
        return PresenceMeasurement(
            Presence.ABSENT, subject, "git",
            f"origin/{branch}'s VERSION differs from this checkout",
            record_commit,
        )

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "merge-base", "--is-ancestor",
        record_commit, remote_commit))
    if rc == 0:
        return PresenceMeasurement(
            Presence.PRESENT, subject, "git",
            f"record commit {record_commit} is published", record_commit,
        )
    if rc == 1:
        return PresenceMeasurement(
            Presence.ABSENT, subject, "git",
            f"record commit {record_commit} is not on origin/{branch}",
            record_commit,
        )
    return PresenceMeasurement(
        Presence.UNKNOWN, subject, "git",
        f"could not compare record commit with origin/{branch}: "
        f"{(err or out).strip()[:400]}",
    )


def ensure_release_record_published(
        eda_root: Path, *, branch: str = "main",
        probe: Optional[Callable[[Path, str], PresenceMeasurement]] = None,
        push: Optional[Callable[[Path, str], Tuple[bool, str]]] = None,
        repair: bool = True,
        ) -> Tuple[bool, dict]:
    """Verify, and when absent retry, release-record publication.

    UNKNOWN never authorises a push.  An explicit ABSENT answer permits the
    existing fail-safe push (which itself refuses a non-ancestor remote), then
    a second read-only probe proves the repair actually reached origin.
    """
    ask = probe or _probe_release_record_publication
    publish = push or push_release_record
    first = ask(eda_root, branch)
    evidence = {
        "initial_state": first.state.value,
        "subject": first.subject,
        "detail": first.detail,
        "identity": first.identity,
        "repair_attempted": False,
    }
    if first.state is Presence.UNKNOWN:
        raise ReleaseMeasurementUnknown(
            stage="release_record_publication",
            category="release_record_remote_unmeasurable",
            subject=first.subject,
            detail=first.detail,
        )
    if first.state is Presence.PRESENT:
        evidence["verified"] = True
        return True, evidence

    if not repair:
        evidence["repair_deferred"] = True
        return False, evidence

    evidence["repair_attempted"] = True
    ok, note = publish(eda_root, branch)
    evidence["repair"] = {"ok": bool(ok), "note": note}
    if not ok:
        return False, evidence

    after = ask(eda_root, branch)
    evidence["final_state"] = after.state.value
    evidence["final_detail"] = after.detail
    evidence["identity"] = after.identity
    if after.state is Presence.UNKNOWN:
        raise ReleaseMeasurementUnknown(
            stage="release_record_publication",
            category="release_record_repair_unmeasurable",
            subject=after.subject,
            detail=after.detail,
        )
    evidence["verified"] = after.state is Presence.PRESENT
    return after.state is Presence.PRESENT, evidence


def authorize_latest_repair(eda_root: Path, version: str,
                            *, branch: str = "main") -> Tuple[bool, dict]:
    """Re-read origin's release record immediately before mutating latest.

    ``_probe_release_record_publication`` compares the fetched branch's current
    RELEASED.json and VERSION bytes with this checkout and proves the record
    commit is on that branch.  Thus a stale noop cannot use an old immutable
    tag as authority after a newer recorded release has advanced origin.
    UNKNOWN remains an exception so callers preserve the unmeasured category.
    """
    measured = _probe_release_record_publication(eda_root, branch)
    evidence = {
        "subject": measured.subject,
        "state": measured.state.value,
        "source": measured.source,
        "detail": measured.detail,
        "identity": measured.identity,
        "expected_version": version,
    }
    if measured.state is Presence.UNKNOWN:
        raise ReleaseMeasurementUnknown(
            stage="noop_publication",
            category="release_authority_unmeasurable",
            subject=measured.subject,
            detail=measured.detail,
        )
    if measured.state is not Presence.PRESENT:
        evidence["summary"] = (
            f"origin/{branch}'s current release record no longer authorises "
            f"repairing :latest to {version}"
        )
        return False, evidence
    try:
        current = (eda_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReleaseMeasurementUnknown(
            stage="noop_publication",
            category="release_authority_unmeasurable",
            subject=str(eda_root / "VERSION"),
            detail=str(exc),
        ) from exc
    if current != version:
        evidence["summary"] = (
            f"local authoritative VERSION is {current!r}, not {version!r}"
        )
        return False, evidence
    evidence["verified"] = True
    return True, evidence


_LIVE_IMAGE_VERSION_RE = re.compile(
    r"(vibeic-eda:)\d+\.\d+\.\d+"
)
_CURRENT_IMAGE_VERSION_RE = re.compile(
    r"(Current:\s*\*\*)\d+\.\d+\.\d+(\*\*)"
)


def _release_changed_paths(eda_root: Path) -> Tuple[Optional[set[str]], str]:
    """Raw index/worktree and non-ignored untracked paths versus ``HEAD``.

    Do not use ``git diff HEAD`` here.  It collapses HEAD->index and
    index->worktree, so equal-and-opposite staged/unstaged changes disappear.
    It also applies clean filters even when external diff and textconv are
    disabled.  The image build reads raw filesystem bytes; admission must judge
    those same bytes, plus the independent index which a later commit replaces.
    """
    head, why = _head_tree_entries(eda_root)
    if head is None:
        return None, why
    indexes, why = _index_entries(eda_root)
    if indexes is None:
        return None, why
    algorithm, why = _object_format(eda_root)
    if algorithm is None:
        return None, why

    changed: set[str] = set()
    for rel in set(head) | set(indexes):
        item = head.get(rel)
        head_signature = ((item[0], item[2]) if item is not None else None)
        indexed = _single_index_entry(indexes, rel)
        values = indexes.get(rel, [])
        if ((values and indexed is None) or indexed != head_signature):
            changed.add(rel)

    for rel, (head_mode, kind, head_oid) in head.items():
        if kind != "blob":
            return None, f"cannot raw-verify non-blob release input HEAD:{rel}"
        working, why = _raw_worktree_blob(eda_root, rel, algorithm)
        if working is None:
            if why:
                return None, why
            changed.add(rel)
            continue
        work_mode, work_oid, _raw = working
        if (work_mode, work_oid) != (head_mode, head_oid):
            changed.add(rel)

    rc, out, err = _sh(_release_git_cmd(
        eda_root, "ls-files", "--others", "--exclude-standard", "-z"),
        timeout=60)
    if rc != 0:
        return None, f"could not enumerate untracked paths: {(err or out)[:200]}"
    untracked = {item for item in out.split("\0") if item}
    changed.update(untracked)
    return changed, ""


def _head_file(eda_root: Path, rel: str) -> Tuple[Optional[str], str]:
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "show", f"HEAD:{rel}"), timeout=60)
    if rc != 0:
        return None, f"could not read HEAD:{rel}: {(err or out).strip()[:200]}"
    return out, ""


def recover_uncommitted_release_record(
        eda_root: Path, version: str, targets: Dict[str, List[str]],
        *, fingerprint: str, immutable_identity: str,
        ) -> Tuple[bool, dict]:
    """Safely commit a proven release record left dirty by an earlier failure.

    This is deliberately narrower than a generic dirty-tree commit.  Recovery
    is authorised by the exact remote immutable identity, the deterministic
    record for this working tree, and a closed set of mechanically-written
    paths.  Any untracked, mixed, deleted, or non-pin edit is a refusal.
    """
    evidence = {
        "attempted": False,
        "version": version,
        "pins_fingerprint": fingerprint,
        "immutable_identity": immutable_identity,
    }

    if (not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)):
        evidence.update({
            "category": "recovery_version_invalid",
            "summary": f"release recovery version is invalid: {version!r}",
        })
        return False, evidence
    if not re.fullmatch(r"[0-9a-f]{64}", immutable_identity or ""):
        evidence.update({
            "category": "recovery_identity_invalid",
            "summary": "release recovery has no exact immutable identity",
        })
        return False, evidence

    remote_ok, remote = verify_remote_immutable_publication(version)
    evidence["immutable_readback"] = remote
    if not remote_ok or remote.get("identity") != immutable_identity:
        evidence.update({
            "category": "recovery_immutable_changed",
            "summary": (f"{RELEASE_IMAGE}:{version} no longer has the "
                        "identity which authorised recovery"),
        })
        return False, evidence

    record_dirty = []
    for rel in ("VERSION", "RELEASED.json"):
        committed, why = _head_file(eda_root, rel)
        if committed is None:
            evidence.update({
                "category": "recovery_head_unmeasurable", "summary": why,
            })
            return False, evidence
        try:
            working = (eda_root / rel).read_text(encoding="utf-8")
        except OSError as exc:
            evidence.update({
                "category": "recovery_record_unreadable",
                "summary": f"could not read working {rel}: {exc}",
            })
            return False, evidence
        if committed != working:
            record_dirty.append(rel)
    evidence["dirty_record_files"] = record_dirty
    if not record_dirty:
        evidence["needed"] = False
        return True, evidence

    # Recovery may publish only the explicit files admitted below.  If local
    # HEAD already contains an unpushed commit, committing the record on top and
    # pushing HEAD would smuggle that commit to main even though it never passed
    # this admission.  Freeze an exact clean base before examining paths.
    remote_base, why = _fetch_remote_branch_commit(eda_root, "main")
    if remote_base is None:
        evidence.update({
            "category": "recovery_base_unmeasurable", "summary": why,
        })
        return False, evidence
    rc, out, err = _sh(_release_git_cmd(
        eda_root, "rev-parse", "--verify", "HEAD^{commit}"), timeout=60)
    local_base = out.strip().lower()
    if (rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", local_base)):
        evidence.update({
            "category": "recovery_base_unmeasurable",
            "summary": ("could not resolve local recovery base: "
                        f"{(err or out).strip()[:200]}"),
        })
        return False, evidence
    evidence["local_base"] = local_base
    evidence["remote_base"] = remote_base
    if local_base != remote_base:
        evidence.update({
            "category": "recovery_base_changed",
            "summary": ("local HEAD is not exactly origin/main; refusing to "
                        "publish unadmitted commits during record recovery"),
        })
        return False, evidence

    changed, why = _release_changed_paths(eda_root)
    if changed is None:
        evidence.update({
            "category": "recovery_changes_unmeasurable",
            "summary": why,
        })
        return False, evidence
    evidence["changed_paths"] = sorted(changed)
    if not changed:
        evidence.update({
            "category": "recovery_changes_inconsistent",
            "summary": "record bytes differ from HEAD but Git reports no change",
        })
        return False, evidence

    try:
        record = released_record(eda_root)
    except ReleaseMeasurementUnknown:
        raise
    expected = release_record_value(
        eda_root, version, targets, manifest_identity=immutable_identity)
    if (record != expected
            or record.get("pins_fingerprint") != fingerprint):
        evidence.update({
            "category": "recovery_record_mismatch",
            "summary": ("working RELEASED.json is not the deterministic record "
                        "for this version, identity, pins, and recipes"),
        })
        return False, evidence
    try:
        version_bytes = (eda_root / "VERSION").read_text(encoding="utf-8")
    except OSError as exc:
        evidence.update({
            "category": "recovery_version_unreadable",
            "summary": f"could not read VERSION for recovery: {exc}",
        })
        return False, evidence
    if version_bytes != version + "\n":
        evidence.update({
            "category": "recovery_version_mismatch",
            "summary": "working VERSION is not the exact recorded version",
        })
        return False, evidence
    if not {"VERSION", "RELEASED.json"}.issubset(changed):
        evidence.update({
            "category": "recovery_record_not_whole",
            "summary": ("recovery requires VERSION and RELEASED.json to be the "
                        "same uncommitted publication"),
        })
        return False, evidence

    allowed_pin_paths = {"Dockerfile", "docker-bake.hcl"}
    allowed_pin_paths.update(
        f"tools/{target}/Dockerfile" for target in targets)
    fixed_paths = {"VERSION", "RELEASED.json", "README.md"}
    unknown = changed - fixed_paths - allowed_pin_paths
    if unknown:
        evidence.update({
            "category": "recovery_mixed_changes",
            "mixed_paths": sorted(unknown),
            "summary": ("release recovery refuses unrelated dirty paths: "
                        + ", ".join(sorted(unknown))),
        })
        return False, evidence

    if "README.md" in changed:
        head_readme, why = _head_file(eda_root, "README.md")
        if head_readme is None:
            evidence.update({
                "category": "recovery_readme_unmeasurable", "summary": why,
            })
            return False, evidence
        expected_readme = _LIVE_IMAGE_VERSION_RE.sub(
            rf"\g<1>{version}", head_readme)
        expected_readme = _CURRENT_IMAGE_VERSION_RE.sub(
            rf"\g<1>{version}\g<2>", expected_readme)
        try:
            working_readme = (eda_root / "README.md").read_text(
                encoding="utf-8")
        except OSError as exc:
            working_readme = None
            why = str(exc)
        if working_readme != expected_readme:
            evidence.update({
                "category": "recovery_readme_mixed",
                "summary": ("README.md contains changes beyond the mechanical "
                            f"version sync{': ' + why if working_readme is None else ''}"),
            })
            return False, evidence

    pin_files = sorted(changed & allowed_pin_paths)
    refused = []
    for rel in pin_files:
        if not (eda_root / rel).is_file():
            refused.append(f"{rel} (missing)")
            continue
        ok, detail = _diff_is_only_pins(eda_root, rel)
        if not ok:
            refused.append(f"{rel} ({detail})")
    if refused:
        evidence.update({
            "category": "recovery_pin_changes_refused",
            "refused_pin_files": refused,
            "summary": ("release recovery found mixed or unmeasurable pin "
                        "changes: " + ", ".join(refused)),
        })
        return False, evidence

    evidence["attempted"] = True
    evidence["admitted_pin_files"] = pin_files
    committed, note = commit_release_record(
        eda_root, version, pin_files)
    evidence["committed"] = bool(committed)
    evidence["commit_note"] = note
    if not committed:
        evidence.update({
            "category": "recovery_commit_failed",
            "summary": note,
        })
        return False, evidence
    evidence["verified"] = True
    return True, evidence


def _require_measured(measurement: PresenceMeasurement, *, stage: str,
                      category: str) -> PresenceMeasurement:
    if measurement.state is Presence.UNKNOWN:
        raise ReleaseMeasurementUnknown(
            stage=stage, category=category, subject=measurement.subject,
            detail=f"{measurement.source} presence unmeasured: "
                   f"{measurement.detail}",
        )
    return measurement


def _existing_versions(eda_root: Path) -> List[Tuple[int, int, int]]:
    """Every local `x.y.z`, or a typed UNKNOWN when Docker did not answer."""
    rc, out, err = _sh(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        timeout=120,
    )
    if rc != 0:
        raise ReleaseMeasurementUnknown(
            stage="version_discovery",
            category="existing_versions_unmeasurable",
            subject="local vibeic-eda version tags",
            detail=(err or out).strip() or (
                f"docker images exited {rc} without a version inventory"
            ),
        )
    seen = set()
    for ln in out.splitlines():
        m = re.search(r"vibeic-eda:(\d+)\.(\d+)\.(\d+)$", ln.strip())
        if m:
            seen.add(tuple(int(g) for g in m.groups()))
    return sorted(seen)


def recipe_hash(eda_root: Path, target: str) -> str:
    """Digest of the Dockerfile and explicitly declared local build inputs.

    An artefact's identity is (source commit, build recipe). The tag carried only
    the first, so a change to HOW a tool is built produced no new tag and the
    release reused the artefact built from the old recipe (#21).

    Demonstrated rather than argued: fixing #18 — verilator's install prefix, a
    pure recipe change with no pin move — left `daily_release` reporting
    "nothing to do", and the corrected Dockerfile could never reach an image.
    Worse, the root `COPY` had already moved to the new path, so the next compose
    would have failed against an artefact that does not contain it.

    Rebuilding under the SAME tag was the tempting shortcut and is the one thing
    that must not happen: every guarantee here rests on a tag naming exactly one
    artefact. So the recipe joins the tag, and a changed recipe simply produces a
    tag that does not exist yet — which is the absence trigger that already works.
    """
    f = eda_root / "tools" / target / "Dockerfile"
    if not f.is_file():
        return "nofile"
    return recipe_digest(f)


def bake_recipe_vars(eda_root: Path) -> Dict[str, str]:
    """What `docker-bake.hcl` currently believes each tool's recipe digest is."""
    hcl = (eda_root / "docker-bake.hcl").read_text(errors="replace")
    return {m.group(1): m.group(2) for m in re.finditer(
        r'variable\s+"([A-Z0-9_]+)_RECIPE"\s*\{\s*default\s*=\s*"([^"]*)"', hcl)}


def write_recipe_vars(eda_root: Path, targets: Dict[str, List[str]]) -> List[str]:
    """Publish each tool's recipe digest into bake. Returns the ones that moved.

    The digest has to live somewhere bake can read because TWO expressions
    compose a tool tag — `tool_tags()` and `eda-local`'s `contexts` map — and a
    digest known only to this program made those two disagree, silently
    disabling the local-build redirect (#21). One value, written here, read by
    both.
    """
    hcl = eda_root / "docker-bake.hcl"
    text = hcl.read_text(errors="replace")
    moved: List[str] = []
    for target in targets:
        var = target.upper().replace("-", "_") + "_RECIPE"
        want = recipe_hash(eda_root, target)
        pat = re.compile(rf'(variable\s+"{var}"\s*\{{\s*default\s*=\s*")[^"]*(")')
        new = pat.sub(rf"\g<1>{want}\g<2>", text)
        if new != text:
            moved.append(f"{var}={want}")
            text = new
    if moved:
        hcl.write_text(text, encoding="utf-8")
    return moved


def compose_recipe_hash(eda_root: Path) -> str:
    """Digest of what builds the COMPOSED image: the root Dockerfile and bake.

    Three levels of this file have now had the same hole. The tool artefacts were
    keyed on the source commit and ignored the tool Dockerfile (#21). The release
    fingerprint was keyed on pins and ignored the tool recipes. And it still
    ignored the root Dockerfile and `docker-bake.hcl`, which are what turn eight
    artefacts into an image — so a fix touching only those, which is what #19 and
    #20 both are, would leave the release reporting "already released" forever.

    Measured before fixing, by appending one comment line to the root Dockerfile:
    the fingerprint did not move.

    `docker-bake.hcl` is included even though `write_recipe_vars` writes to it —
    that write runs first and is idempotent, so the digest is stable across runs
    that change nothing.
    """
    h = hashlib.sha256()
    for name in ("Dockerfile", "docker-bake.hcl"):
        f = eda_root / name
        h.update(f.read_bytes() if f.is_file() else b"missing")
    return h.hexdigest()[:6]


def artefact_tag(eda_root: Path, target: str, ref_vars: List[str],
                 refs: Dict[str, str]) -> Optional[str]:
    """The full artefact reference: every source commit, then the recipe."""
    shorts = [refs[v][:SHORT] for v in ref_vars if v in refs]
    if len(shorts) != len(ref_vars):
        return None
    return (f"ghcr.io/vibeic/eda-tool-{target}:"
            f"{'-'.join(shorts)}-{recipe_hash(eda_root, target)}")


def missing_artefacts(eda_root: Path, refs_now: Dict[str, str],
                     targets: Dict[str, List[str]]) -> Dict[str, str]:
    """target -> tag, for every pinned artefact that does not exist yet.

    THE REBUILD TRIGGER IS ABSENCE, NOT MOVEMENT. An earlier version rebuilt
    whatever moved DURING THIS RUN, which is not the same question and fails in
    the one case that matters: a run interrupted between moving the pin and
    building the artefact. That happened on 2026-07-29 — the pins were rewritten,
    the OpenROAD build was killed, and the next run said "every pin is already
    its fork's tip, nothing to do" while the artefact for that pin did not exist
    anywhere. Exactly the state the whole chain is built to prevent, produced by
    the chain itself.

    Asking whether the artefact EXISTS makes the release idempotent and
    self-healing: interrupt it anywhere, run it again, and it resumes.
    """
    missing: Dict[str, str] = {}
    for target, ref_vars in targets.items():
        ref = artefact_tag(eda_root, target, ref_vars, refs_now)
        if ref is None:
            continue
        local = _require_measured(
            _probe_local_image(ref), stage="artefact_discovery",
            category="local_artefact_state_unmeasurable",
        )
        if local.state is Presence.PRESENT:
            continue
        remote = _require_measured(
            _probe_remote_manifest(ref), stage="artefact_discovery",
            category="registry_artefact_state_unmeasurable",
        )
        if remote.state is Presence.PRESENT:
            continue
        missing[target] = ref
    return missing


#: What each tool must do inside the composed image before it is called a
#: release. Each entry RUNS the tool and asserts the binary is the one WE built:
#: a bare `command -v` passes on the base image's copy, and `klayout -v` prints a
#: version string for whichever klayout answers first. Measured 2026-07-29:
#: `command -v klayout` resolved the base image's binary while our fork lives at
#: /foss/tools/klayout-vibeic — the check was green and the fork was not loaded.
SMOKE = {
    "yosys":     "yosys -V | grep -qi yosys",
    "openroad":  "openroad -version",
    # Our klayout fork ships as libraries + add-on engines, not as a `klayout`
    # binary — see vibeic-eda#17: the `klayout` on PATH is the BASE image's build
    # and links the base library. So this asserts our fork's library is present;
    # it deliberately does NOT claim the fork is what `klayout` loads, because it
    # is not.
    "klayout":   "test -e /foss/tools/klayout-vibeic/libklayout_db.so",
    "iverilog":  "iverilog -V >/dev/null",
    "verilator": "verilator --version | grep -qi verilator",
    "ngspice":   "ngspice --version >/dev/null 2>&1 || command -v ngspice",
    "magic":     "magic --version >/dev/null 2>&1 || command -v magic",
    "netgen":    "command -v netgen",
    "cocotb":    "python3 -c 'import cocotb; print(cocotb.__version__)'",
    "pyuvm":     "python3 -c 'import pyuvm'",
    "sby":       "sby --help >/dev/null 2>&1 || command -v sby",
    # LibreLane is only proven by running a flow. 0.3.80 shipped with every
    # librelane import check green and died in EVERY Classic flow at
    # Magic.StreamOut (AttributeError: 'Path' object has no attribute
    # 'read_bytes' -- the fork's GDS substance check was handed librelane's own
    # Path, not a pathlib.Path). The build-time check drives StreamOut.run with
    # a stubbed Magic; this one runs the real Classic steps, spm x sky130A,
    # through Magic.StreamOut (about 1.5 min) and asserts the streamed GDS.
    "librelane": (
        'cd "$(mktemp -d)" && python3 -P -c \''
        'import os, shutil, librelane; '
        'from librelane.flows import Flow, SequentialFlow; '
        'from librelane.state import DesignFormat as DF; '
        'C = Flow.factory.get("Classic"); ids = [s.id for s in C.Steps]; '
        'Smoke = type("SmokeThroughMagicStreamOut", (SequentialFlow,), '
        '{"Steps": C.Steps[:ids.index("Magic.StreamOut") + 1]}); '
        'shutil.copytree(os.path.join(os.path.dirname(librelane.__file__), '
        '"examples", "spm"), "spm"); '
        'st = Smoke(os.path.abspath("spm/config.yaml"), '
        'design_dir=os.path.abspath("spm"), pdk="sky130A", '
        'pdk_root=os.environ.get("PDK_ROOT", "/foss/pdks")).start(tag="smoke"); '
        'g = st.get(DF.MAG_GDS); '
        'assert g is not None and os.path.getsize(str(g)) > 0, g; '
        'print("librelane: spm x sky130A through Magic.StreamOut ->", g)\''
    ),
}


def smoke_image(image: str) -> List[dict]:
    """Run every tool in the composed image. Returns the FAILURES only.

    This proves each tool starts and is the build we shipped. It does not prove
    any of them is correct — that is the regression suite's job. A release that
    ships a tool which cannot start is a different and much cheaper failure to
    catch, and until now nothing caught it before the version was cut.
    """
    bad: List[dict] = []
    for tool, cmd in sorted(SMOKE.items()):
        # Docker work happens outside the client process tree. Reuse the
        # capability gate's container-aware lease: output or the container's
        # actual host process-tree CPU may advance for any length of time, and
        # the container is removed on a flat lease. These are liveness signals,
        # never a verdict; only a natural OS rc is judged below.
        run = run_container_probe(image, cmd, SMOKE_STALL_GRACE_S)
        if run.rc != 0:
            outcome = ("natural_failure" if run.outcome == "natural"
                       else run.outcome)
            detail = run.output.strip()[-200:]
            bad.append({"tool": tool, "cmd": cmd, "err": detail,
                        "outcome": outcome, "rc": run.rc})
            # A stalled Docker arm is an infrastructure/protocol NORECORD.  One
            # is sufficient to refuse this release; serially waiting for the
            # same wedged daemon ten more times adds no evidence.
            if run.outcome != "natural":
                break
    return bad


def _next_version(version: str) -> str:
    fields = version.split(".")
    if len(fields) != 3 or any(not field.isdigit() for field in fields):
        raise ValueError(f"invalid release version {version!r}")
    major, minor, patch = (int(field) for field in fields)
    patch += 1
    if patch > 99:
        minor, patch = minor + 1, 0
    return f"{major}.{minor}.{patch}"


def peek_version(eda_root: Path,
                 existing_versions: Optional[List[Tuple[int, int, int]]] = None,
                 ) -> Tuple[str, str]:
    """Next version, above BOTH the VERSION file and every tag that exists.

    The file drifts. Measured 2026-07-29: `VERSION` read 0.2.30 while images
    0.2.31 and 0.2.32 were already built and tagged — two releases that never
    wrote the file back. Bumping the file alone would have re-issued 0.2.31 over
    a different image, which is the one thing a version number must never do.
    """
    vf = eda_root / "VERSION"
    old = vf.read_text().strip()
    cur = tuple(int(x) for x in (old.split(".") + ["0", "0"])[:3])
    measured = (_existing_versions(eda_root)
                if existing_versions is None else existing_versions)
    top = max([cur] + measured)
    # DECIDED, NOT WRITTEN. The file moves only after the image is smoked and
    # pushed — a VERSION that advanced for an image nobody can pull is how the
    # file drifted from reality in the first place (0.2.30 on disk, 0.2.32 built).
    return old, _next_version(".".join(str(part) for part in top))


def select_publish_version(
        eda_root: Path,
        existing_versions: Optional[List[Tuple[int, int, int]]] = None,
        *, max_registry_probes: int = 10_000,
        ) -> Tuple[str, str, List[str]]:
    """Choose a version whose remote manifest is explicitly absent.

    A local inventory cannot establish that a registry tag is unused.  This is
    how a host with no local 0.3.0 image could otherwise overwrite an existing
    remote 0.3.0.  PRESENT candidates are skipped; UNKNOWN is a refusal, never
    permission to publish.  The returned list records occupied candidates.
    """
    old, candidate = peek_version(eda_root, existing_versions)
    occupied: List[str] = []
    for _ in range(max_registry_probes):
        tag = f"ghcr.io/vibeic/vibeic-eda:{candidate}"
        measured = _require_measured(
            _probe_remote_manifest(tag), stage="version_discovery",
            category="candidate_tag_state_unmeasurable",
        )
        if measured.state is Presence.ABSENT:
            return old, candidate, occupied
        occupied.append(candidate)
        candidate = _next_version(candidate)
    raise ReleaseMeasurementUnknown(
        stage="version_discovery",
        category="candidate_space_unmeasurable",
        subject="vibeic-eda version candidate",
        detail=(f"the first {max_registry_probes} candidates all exist; "
                "refusing to guess beyond the measured search window"),
    )


def _main_under_release_lock(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true",
                    help="move pins and stop; do not build or version")
    ap.add_argument("--json", default=None)
    ap.add_argument("--smoke-only", default=None, metavar="IMAGE",
                    help="run the tool smoke against an existing image and stop")
    ap.add_argument("--repair-latest", default=None, metavar="VERSION",
                    help="make :latest name the already-published VERSION "
                         "manifest, through the same authorization the release "
                         "path uses. For the state a raced record push leaves "
                         "behind (vibeic-eda#194): :latest stale -> "
                         "test_release_record_pins_ahead RED -> semantic pytest "
                         "RED -> run_tick NOT CHECKED -> daily_release never "
                         "runs -> the noop arm that repairs :latest never runs. "
                         "Refuses unless VERSION is the recorded version and "
                         "authorize_latest_repair agrees.")
    ap.add_argument("--record-release", default=None, metavar="VERSION",
                    help="write RELEASED.json for a version published OUTSIDE "
                         "this program (vibeic-eda#51), through the same writer "
                         "the publish path uses. Refuses when VERSION does not "
                         "match the VERSION file, because the record describes "
                         "the tree it is written from.")
    a = ap.parse_args(argv)
    root = Path(a.eda_root)
    result = {
        "program": "daily_release",
        "schema_version": 2,
        "status": "running",
        "started_at": _utcnow(),
        "moved": [],
        "upstream_bump_available": [],
        "undecidable": [],
        "unresolved": [],
        "built": [],
        "version": None,
        "compose_attempts": [],
        "smoke": {
            "executed": False,
            "expected": len(SMOKE),
            "passed": 0,
            "outcome": "not_applicable",
            "failures": [],
        },
    }
    # Replace yesterday's terminal record before doing anything that can return
    # early.  A crash can now leave "running" (incomplete and visibly so), never
    # a prior day's success masquerading as this run's answer.
    if a.json:
        _atomic_json(a.json, result)

    if a.repair_latest:
        result["mode"] = "repair-latest"
        rec, rec_err = released_record(root), None
        recorded = rec.get("version") if isinstance(rec, dict) else None
        if recorded != a.repair_latest:
            print(f"[REFUSED] --repair-latest {a.repair_latest} but "
                  f"RELEASED.json records {recorded!r}. :latest may only be "
                  f"pointed at the version this tree's record authorises.",
                  file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "repair-latest", "category": "version_mismatch",
                "retryable": False,
                "summary": f"RELEASED.json records {recorded!r}, "
                           f"not {a.repair_latest!r}",
            })
        try:
            auth_ok, auth = authorize_latest_repair(root, a.repair_latest)
        except ReleaseMeasurementUnknown as exc:
            return _finish_unmeasured(result, a.json, exc)
        result["latest_authority"] = auth
        if not auth_ok:
            print(f"[REFUSED] {auth.get('summary', 'release authority changed')}",
                  file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "repair-latest", "category": "release_authority_changed",
                "retryable": True,
                "summary": auth.get("summary", "release authority changed"),
            })
        if a.dry_run:
            try:
                _ok, ev = repair_latest_publication(
                    a.repair_latest,
                    expected_identity=rec.get("manifest_identity"),
                    repair=False)
            except ReleaseMeasurementUnknown as exc:
                return _finish_unmeasured(result, a.json, exc)
            result["remote_publication"] = ev
            print(f"[DRY RUN] :latest would be repaired to {a.repair_latest}; "
                  f"nothing was pushed ({ev.get('category') or 'already correct'})")
            return _finish(result, a.json, RC_OK, "dry_run")
        try:
            ok, ev = repair_latest_publication(
                a.repair_latest,
                expected_identity=rec.get("manifest_identity"))
        except ReleaseMeasurementUnknown as exc:
            return _finish_unmeasured(result, a.json, exc)
        result["remote_publication"] = ev
        if not ok:
            print(f"[FAIL] :latest was not repaired: "
                  f"{ev.get('summary') or ev.get('category')}", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "repair-latest",
                "category": ev.get("category", "latest_repair_failed"),
                "retryable": bool(ev.get("retryable", True)),
                "summary": ev.get("summary", ":latest was not repaired"),
            })
        print(f":latest now identifies {a.repair_latest} "
              f"({str(ev.get('frozen_identity'))[:12]}…)")
        return _finish(result, a.json, RC_OK, "repaired")

    if a.record_release:
        result["mode"] = "record-release"
        # The record is computed FROM THE TREE, so recording a version this tree
        # is not is recording a pin set that version was never built from — the
        # #51 defect with the numbers swapped.
        vf = (root / "VERSION")
        cur = vf.read_text().strip() if vf.is_file() else None
        if cur != a.record_release:
            print(f"[REFUSED] --record-release {a.record_release} but VERSION "
                  f"says {cur!r}. The record is computed from THIS tree; "
                  f"writing another version's name onto it records a pin set "
                  f"that version was never built from.", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "record-release", "category": "version_mismatch",
                "retryable": False,
                "summary": f"VERSION says {cur!r}, not {a.record_release!r}",
            })
        if a.dry_run:
            # ``--dry-run`` is global, including the early maintenance modes.
            # Previously this branch bypassed the release-path guard and wrote
            # RELEASED.json while claiming to be a dry run.
            print(f"[DRY RUN] would record {a.record_release} from this tree; "
                  "RELEASED.json was not written")
            result["version"] = {"to": a.record_release}
            return _finish(result, a.json, RC_OK, "dry_run")
        rec = write_released_record(root, a.record_release, bake_targets(root))
        print(f"RELEASED.json <- {rec['version']} "
              f"fingerprint={rec['pins_fingerprint']} "
              f"({len(rec['pins'])} pin(s))")
        print("  NOTE: this records what THIS TREE would build. Verify the "
              "published image was built from it — `check_release_recorded` "
              "asks the registry, and the image's own "
              "/vibeic/provenance/*.json name the refs it actually used.")
        result["version"] = {"to": a.record_release}
        return _finish(result, a.json, RC_OK, "recorded")

    if a.smoke_only:
        result["mode"] = "smoke-only"
        bad = smoke_image(a.smoke_only)
        for b in bad:
            print(f"  FAIL {b['tool']}: {b['cmd']}\n    {b['err']}",
                  file=sys.stderr)
        print(f"smoke: {len(SMOKE) - len(bad)}/{len(SMOKE)} tools run in "
              f"{a.smoke_only}")
        result["smoke_failures"] = bad
        result["smoke"] = {
            "executed": True,
            "expected": len(SMOKE),
            "passed": len(SMOKE) - len(bad),
            "outcome": "natural_failure" if bad else "natural",
            "failures": bad,
        }
        if bad:
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "smoke", "category": "tool_smoke",
                "retryable": False,
                "summary": f"{len(bad)} tool(s) failed smoke",
            })
        return _finish(result, a.json, RC_OK, "smoke_passed")

    # COHERENCE BEFORE ANYTHING ELSE (vibeic-eda#75 follow-up). A pin stated
    # three different ways must state the same thing before it is worth asking
    # whether it is CURRENT — otherwise this program moves a variable while the
    # image keeps pulling the old tag, which is what it just did to sv-elab.
    #
    # `check_pins_agree` already existed and already caught it. It was wired
    # ONLY into `.github/workflows/release.yml`, and Actions is disabled at the
    # account level (vibe-ic#550) — MEASURED: `gh run list` reports ZERO runs of
    # any workflow in this repo, ever. So the gate produced no verdict, the
    # dry-run said "nothing to do", and the disagreement was found by running it
    # by hand. A gate wired to a rail that never moves is the #693 shape.
    source_guards = (
        ("check_pins_agree.py",
         "the pins disagree with themselves; a release cut here ships something "
         "other than what it says."),
        ("check_dockerfile_layer_budget.py",
         "the Dockerfile has spent the image's pull-depth reserve; compact the "
         "source layout before composing another release."),
    )
    for guard, refusal in source_guards:
        checked = subprocess.run([sys.executable, str(root / "tools" / guard)],
                                 capture_output=True, text=True)
        if checked.returncode != 0:
            sys.stdout.write(checked.stdout)
            sys.stderr.write(checked.stderr)
            print(f"[REFUSED] {refusal}", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "source_guard", "category": guard,
                "retryable": False, "summary": refusal,
            })

    pins = pinned_refs(root)
    args_of = ref_arg_names(root)
    targets = bake_targets(root)
    if not pins:
        print("[NOT CHECKED] no pins found — nothing was released, which is not "
              "a clean result", file=sys.stderr)
        return _finish(result, a.json, RC_NOTHING, "not_checked", {
            "stage": "pin_discovery", "category": "no_pins",
            "retryable": False, "summary": "no pins found",
        })

    # Resolution goes through `check_one`, which VERIFIES the branch contains the
    # pin. An earlier version called `build_branch` — which ranks branches by
    # naming convention and does not check containment — and then took its tip.
    # On the first real run that proposed moving cadical's pin onto an upstream
    # `sweep` branch, netgen's onto `vibeic/lvs-fidelity` and ALIGN's onto
    # `place_by_constraint`: four unrelated lines of development, presented as
    # "these tools have a new version". Nothing caught it but reading the diff.
    #
    # Only a STALE verdict moves a pin. CURRENT is nothing to do; anything else
    # is a pin we could not resolve, and an unresolved pin is reported, never
    # bumped on a guess.
    moved, unresolved, upstream_bump, undecidable = [], [], [], []
    for repo, pin in sorted(pins.items()):
        v = check_one(repo, pin)
        if v["verdict"] == "CURRENT":
            continue
        if v["verdict"] == "UPSTREAM_AVAILABLE":
            # A REGRESSION I INTRODUCED ONE COMMIT EARLIER. vibeic-eda#29 split
            # `check_one`'s STALE into STALE / UPSTREAM_AVAILABLE /
            # STALE_UNDECIDED, and this branch tests `!= "STALE"`, so the new
            # verdict fell into "could not resolve" and the release started
            # reporting `[NEEDS HUMAN] 4 pin(s) could not be resolved` where it
            # had printed nothing to do.
            #
            # The state already exists here — `upstream_bump` is this program's
            # own name for the same fact — so the fix is to route it there, not
            # to widen the STALE test. Fixing a shared vocabulary in one program
            # and matching on the old vocabulary in the other is the same defect
            # #29 was about, one commit later.
            # THE PURE-MIRROR CASE REACHES THIS BRANCH FIRST (vibeic-eda#106).
            #
            # `_is_pure_mirror` was added to the `branch_is_ours is False` arm
            # further down, and it never fired: `UPSTREAM_AVAILABLE` short-
            # circuits here with a `continue`, so ORFS and slang -- the two forks
            # the fix exists for -- were intercepted before reaching it. Measured
            # by running the release: it still printed "NOT MOVED — that branch
            # carries none of our commits" for both, with the fix landed.
            #
            # That is worth naming: a guard added to one of two paths that reach
            # the same outcome is a guard that has not been added. The two paths
            # print the SAME sentence, so the log gave no hint which one produced
            # it.
            if _is_pure_mirror(repo):
                _tip = _gh_tip(repo, v.get("branch"))
                if _tip:
                    moved.append({"repo": repo, "branch": v.get("branch"),
                                  "arg": args_of.get(repo),
                                  "from": pin[:9], "to": _tip[:9], "sha": _tip,
                                  "behind": v.get("behind"),
                                  "branch_is_ours": False, "pure_mirror": True})
                    continue
            upstream_bump.append({"repo": repo, "branch": v.get("branch"),
                                  "from": pin[:9], "to": "(not moved)",
                                  "behind": v.get("behind"),
                                  "branch_is_ours": False})
            continue
        if v["verdict"] in ("PIN_OFF_DECLARED_BRANCH", "DECLARED_BRANCH_ABSENT"):
            # The pin is not on the line FORKS.json declares for this fork, or
            # that line does not exist.  Never advance on a guess: measured
            # 2026-09-07, check_one answered `vibeic/antenna-best-iteration`
            # for OpenROAD's pin (a topic branch that also contained it) and
            # this loop moved the pin to that branch's tip -- a commit on no
            # declared line -- and the next round's daily_merge refused every
            # release with NO_BRANCH_AT_PIN.  Named here, not folded into the
            # generic arm below, so the log says what to do: put the pin back
            # on the declared branch first.
            unresolved.append({"repo": repo, "verdict": v["verdict"],
                               "why": f"{v['verdict']}: {v['detail']}"})
            continue
        if v["verdict"] != "STALE":
            unresolved.append({"repo": repo, "verdict": v["verdict"],
                               "why": f"{v['verdict']}: {v['detail']}"})
            continue
        tip = _gh_tip(repo, v["branch"])
        if not tip:
            unresolved.append({"repo": repo, "verdict": "TIP_LOOKUP_UNKNOWN",
                               "why": f"tip of {v['branch']} unreadable"})
            continue
        ours = branch_is_ours(repo, v["branch"])
        row = {"repo": repo, "branch": v["branch"], "arg": args_of.get(repo),
               "from": pin[:9], "to": tip[:9], "sha": tip,
               "behind": v.get("behind"), "branch_is_ours": ours}
        if ours:
            moved.append(row)
        elif ours is False and _is_pure_mirror(repo):
            # A PURE MIRROR HAS NOTHING TO PROTECT (vibeic-eda#106).
            #
            # `branch_is_ours` guards a real thing: vibeic-eda#23/#25 pinned
            # slang, xschem, Xyce and sv-elab to the commits the IMAGE SHIPS, and
            # the next release wanted to move all four to `master` -- a four-tool
            # version bump smuggled under "build 6 absent artefacts". Refusing
            # that is right, and this does not change it.
            #
            # But it refuses on ONE branch carrying none of our commits, and for a
            # fork that carries none ANYWHERE the refusal is unconditional: the
            # pin can never advance, so the fleet can never converge. Measured
            # 2026-08-06: OpenROAD-flow-scripts and slang are each 0 ahead of
            # upstream and 0 behind it -- exactly level -- while their pins
            # trailed by 18 commits, and `release 18` had to be closed by hand
            # every time. A gatekeeper decision that is the same decision every
            # day is not a decision, it is a chore the program refused to do.
            #
            # For such a fork the pin's only meaning is "which upstream commit do
            # we build", and advancing it IS tracking upstream. The distinction
            # is measured per FORK, not per branch: carry one commit of ours
            # anywhere and the old refusal applies again, unchanged.
            row["pure_mirror"] = True
            moved.append(row)
        elif ours is False:
            # Not stale — a deliberate pin on upstream history, in a fork that
            # DOES carry work of ours elsewhere. Reported so it is visible, never
            # advanced on its own.
            upstream_bump.append(row)
        else:
            # UNKNOWN IS NOT A NEGATIVE. This used to fall in with the branch
            # above and print "that branch carries none of our commits" — a
            # statement of fact asserted from a `None`, about branches like
            # `yosys satfix-integration` that exist nowhere but our fork. Both
            # outcomes leave the pin alone; only one of them is entitled to say
            # why.
            undecidable.append(row)

    # Mapping a fork to no Dockerfile ARG is also a missing release input.  It
    # used to be discovered only inside the mutation block, after recipe files
    # had already been rewritten.  Resolve every prospective writer target now
    # so an incomplete pin set cannot produce a partial release.
    for m in moved:
        if not m.get("arg"):
            unresolved.append({"repo": m["repo"], "verdict": "PIN_SITE_MISSING",
                               "why": "no ARG <NAME>_REF pins it"})

    print(f"daily_release: {len(pins)} pin(s), {len(moved)} tool(s) with a new "
          f"version, {len(upstream_bump)} awaiting an upstream decision, "
          f"{len(undecidable)} undecidable, {len(unresolved)} unresolved")
    for u in upstream_bump:
        print(f"  {u['repo']:<20} {u['from']} -> {u['to']}  ({u['branch']}) "
              f"NOT MOVED — that branch carries none of our commits, so this is "
              f"an upstream version bump, not a stale pin")
    for u in undecidable:
        print(f"  {u['repo']:<20} {u['from']} -> {u['to']}  ({u['branch']}) "
              f"NOT MOVED — could not determine whether that branch carries our "
              f"commits. The pin is left alone because unknown is not a licence "
              f"to move it, NOT because the branch was found to be upstream's.",
              file=sys.stderr)
    for m in moved:
        print(f"  {m['repo']:<20} {m['from']} -> {m['to']}  ({m['branch']})")
    for u in unresolved:
        print(f"  {u['repo']:<20} UNRESOLVED — {u['why']}", file=sys.stderr)

    result.update({"moved": moved,
                   "upstream_bump_available": upstream_bump,
                   "undecidable": undecidable,
                   "unresolved": unresolved})
    if a.json:
        _atomic_json(a.json, result)

    # Ownership is an input to the mutation, not an advisory printed after it.
    # A transport failure used to become ``True`` inside ``branch_is_ours``;
    # even after that predicate became tri-state, this program merely listed
    # ``undecidable`` and continued to rewrite other pins, build, and push.  A
    # partial release made while one ownership measurement is absent is not a
    # converged release.  Stop before the first writer (`write_recipe_vars`) and
    # preserve the distinction from a measured rejection in the sidecar.
    if undecidable:
        print(f"[UNMEASURED] ownership of {len(undecidable)} stale branch(es) "
              "could not be established; no pin, build, or image was changed",
              file=sys.stderr)
        return _finish(result, a.json, RC_NEEDS_HUMAN, "unmeasured", {
            "stage": "pin_ownership",
            "category": "ownership_unmeasurable",
            "retryable": True,
            "summary": (f"ownership of {len(undecidable)} stale branch(es) "
                        "was not measured"),
        })

    # Pin resolution is an authorization input, not an after-the-fact audit.
    # Previously this refusal lived at the end of the function, after
    # write_recipe_vars/rewrite_pin/build/push.  A red exit cannot undo a tag
    # that was already published, so stop before the first writer instead.
    if unresolved:
        # Some unresolved states are completed negative measurements (the fork
        # authoritatively has no branches / the pin is orphaned); others mean a
        # required API or comparison never formed a record.  Both block before
        # writers, but only the latter may honestly be called unmeasured.
        unmeasured_kinds = {
            "BRANCH_LOOKUP_UNKNOWN": ("pin_resolution",
                                      "branch_lookup_unmeasurable"),
            "COMPARE_FAILED": ("pin_resolution", "pin_compare_unmeasurable"),
            "STALE_UNDECIDED": ("pin_ownership", "ownership_unmeasurable"),
            "TIP_LOOKUP_UNKNOWN": ("pin_resolution", "tip_lookup_unmeasurable"),
        }
        measured_negative = {
            "NO_BRANCHES": ("pin_resolution", "no_branches"),
            "ORPHANED": ("pin_resolution", "orphaned_pin"),
            "PIN_SITE_MISSING": ("pin_resolution", "pin_site_missing"),
        }
        first = unresolved[0].get("verdict", "UNKNOWN_VERDICT")
        any_unmeasured = next(
            (row.get("verdict") for row in unresolved
             if row.get("verdict") not in measured_negative), None)
        if any_unmeasured:
            stage, category = unmeasured_kinds.get(
                any_unmeasured, ("pin_resolution", "pin_resolution_unmeasurable"))
            status, retryable, label = "unmeasured", True, "UNMEASURED"
        else:
            stage, category = measured_negative.get(
                first, ("pin_resolution", "unresolved_pins"))
            status, retryable, label = "failed", False, "REFUSED"
        print(f"[{label}] {len(unresolved)} pin(s) could not be resolved; "
              "no pin, build, or image was changed", file=sys.stderr)
        return _finish(result, a.json, RC_NEEDS_HUMAN, status, {
            "stage": stage, "category": category,
            "retryable": retryable,
            "summary": f"{len(unresolved)} pin(s) could not be resolved",
        })

    # THE COMPOSED IMAGE IS AN ARTEFACT TOO. Checking only the per-tool
    # artefacts left the exact hole this program is about, one level up: after a
    # run built and published both tools and was then interrupted before
    # composing, the next run said "every pin is its fork's tip and every pinned
    # artefact exists — nothing to rebuild" while no image had ever been made
    # from those pins. Reproduced it on 2026-07-29, in this file, hours after
    # fixing the same shape for tool artefacts.
    #
    # An image tag is a version number, not a content hash, so "does the image
    # exist" cannot be asked of the registry. RELEASED.json records what the last
    # published image was built FROM; a pin set that does not match it has never
    # been released.
    # The fingerprint covers pins AND recipes, because an image is built from
    # both. Over pins alone, 0.2.33 and 0.2.34 — identical pins, different
    # verilator recipe, different bytes — would share a fingerprint, and
    # RELEASED.json would be unable to say which one it recorded. It would also
    # report a recipe-only change as "already released", which is exactly the
    # #18 shape one layer up.
    fp = pins_fingerprint({**pins,
                           **{f"recipe:{k}": recipe_hash(root, k)
                              for k in targets},
                           "recipe:__compose__": compose_recipe_hash(root)})
    try:
        rec = released_record(root)
    except ReleaseMeasurementUnknown as exc:
        return _finish_unmeasured(result, a.json, exc)
    version_file = root / "VERSION"
    current_version = (version_file.read_text().strip()
                       if version_file.is_file() else None)
    result["version"] = {
        "from": rec.get("version"),
        "to": current_version,
    }
    unreleased = rec.get("pins_fingerprint") != fp
    if unreleased:
        print(f"  pin set {fp} has not been released "
              f"(last released: {rec.get('pins_fingerprint') or 'never'})")

    refs_all = {f"{args_of[r]}_REF": s for r, s in pins.items() if r in args_of}
    # Computed on a dry run too: `docker image/manifest inspect` only reads, and
    # a preview that cannot see a missing artefact previews a rosier release than
    # the real one.
    try:
        absent = missing_artefacts(root, refs_all, targets)
    except ReleaseMeasurementUnknown as exc:
        return _finish_unmeasured(result, a.json, exc)
    if absent:
        print(f"  {len(absent)} pinned artefact(s) do not exist yet: "
              f"{', '.join(sorted(absent))}")
    result["absent_artefacts"] = sorted(absent)

    # Version inventory is an input to the FIRST candidate number, so measure
    # it before the first writer.  Previously `docker images` failure became an
    # empty list, then VERSION 0.2.99 selected 0.3.0 even when that tag already
    # existed.  A missing inventory is now retained as UNKNOWN and reaches no
    # pin rewrite, artefact build, compose, or push.
    existing_versions: Optional[List[Tuple[int, int, int]]] = None
    selected_version: Optional[Tuple[str, str]] = None
    release_needed = bool(moved or absent or unreleased)
    if release_needed and not a.dry_run and not a.no_build:
        try:
            existing_versions = _existing_versions(root)
            old_candidate, new_candidate, occupied = select_publish_version(
                root, existing_versions,
            )
        except ReleaseMeasurementUnknown as exc:
            return _finish_unmeasured(result, a.json, exc)
        selected_version = (old_candidate, new_candidate)
        result["occupied_version_candidates"] = occupied

    # A new immutable image must be reproducible from the exact Git tree which
    # will carry its release record.  Recipe hashes deliberately read working
    # files, so without this admission a human Dockerfile/README edit (or an
    # untracked source input) could enter the image while the later explicit
    # release commit omitted it.  Enumerate both index/worktree changes and all
    # non-ignored untracked paths through the fixed, sanitised Git invocation.
    #
    # This belongs after the read-only release measurements above and before
    # the first writer below.  The proven failed-publication recovery takes the
    # noop path and performs its own narrower mixed-change admission instead.
    if release_needed and not a.dry_run:
        source_changes, source_why = _release_changed_paths(root)
        if source_changes is None:
            result["source_admission"] = {
                "verified": False,
                "changed_paths": None,
                "detail": source_why,
            }
            print("[UNMEASURED] release source cleanliness could not be "
                  "established; no recipe, pin, build, or image was changed",
                  file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "unmeasured", {
                "stage": "release_source_admission",
                "category": "release_source_state_unmeasurable",
                "retryable": True,
                "summary": source_why,
            })
        result["source_admission"] = {
            "verified": not source_changes,
            "changed_paths": sorted(source_changes),
        }
        if source_changes:
            detail = ", ".join(sorted(source_changes))
            print("[REFUSED] release source contains tracked or untracked "
                  f"changes ({detail}); no recipe, pin, build, or image was "
                  "changed", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "release_source_admission",
                "category": "release_source_dirty",
                "retryable": False,
                "summary": ("new release requires a clean source tree; "
                            f"dirty paths: {detail}"),
            })

    # Publish the recipe digests only after every pre-mutation measurement has
    # formed a record.  `artefact_tag` hashes the source Dockerfile directly, so
    # the registry questions above do not depend on this write.
    pin_files: List[str] = []
    stale = [] if a.dry_run else write_recipe_vars(root, targets)
    for s in stale:
        print(f"  recipe changed: {s}")
    if stale:
        # This first call is the only write in a recipe-only release.  The
        # second call below is intentionally idempotent, so discovering the
        # path only there left docker-bake.hcl dirty and absent from the release
        # commit while RELEASED.json fingerprinted its working-tree bytes.
        pin_files.append("docker-bake.hcl")

    if (moved or absent or unreleased) and not a.dry_run:
        for m in moved:
            m["files"] = rewrite_pin(root, m["arg"], m["sha"])
        refs_now = {f"{args_of[r]}_REF": s for r, s in
                    {**pins, **{m["repo"]: m["sha"] for m in moved}}.items()
                    if r in args_of}
        # RE-PUBLISH the recipe digests, because `rewrite_pin` just edited the
        # very files they are the hash OF.
        #
        # The call above runs before anything reads a tag, which is right for
        # everything between there and here. But a pin lives INSIDE
        # `tools/<t>/Dockerfile`, so moving it necessarily moves
        # `sha256(tools/<t>/Dockerfile)[:6]` — and the digest published a moment
        # ago is now describing a file that no longer exists in that form.
        #
        # `retag_images` below recomputes the tag from the CURRENT file, so
        # without this the two sites disagree by construction on every release
        # that moves a pin: `docker-bake.hcl` keeps the pre-edit digest and the
        # composing Dockerfile gets the post-edit one. That is the exact
        # `check_pins_agree` failure the 2026-08-05 tick reported for openroad,
        # yosys and verilator — "bake would publish a tag the composing
        # Dockerfile does not pull" — reported AFTER the release, by which point
        # the round had already failed to compose.
        # The pin files THIS RUN wrote, accumulated as they are written so the
        # record commit can name them by explicit path rather than re-derive
        # "which files hold a pin" — a second list that would drift from the
        # first. `rewrite_pin` already returns the paths it changed; the other
        # two writers report WHAT moved, not where, and each owns one file.
        pin_files.extend(
            f for m in moved for f in (m.get("files") or []))

        for s2 in write_recipe_vars(root, targets):
            print(f"  recipe changed (after pin move): {s2}")
            pin_files.append("docker-bake.hcl")

        result["retagged"] = retag_images(root, refs_now, targets)
        for t in result["retagged"]:
            print(f"  retag {t}")
            pin_files.append("Dockerfile")

        if not a.no_build:
            builder = preferred_builder()
            bflag = ["--builder", builder] if builder else []
            if builder:
                print(f"  building on `{builder}` (docker-container), so a "
                      f"long step's log is kept in full")
            eff = log_limit_effective(builder)
            if eff is False:
                print("  NOTE: the buildx driver is `docker`, so buildkit runs "
                      "inside dockerd and BUILDKIT_STEP_LOG_MAX_SIZE cannot "
                      "reach it from here — a long step's log WILL be clipped "
                      "at 2 MiB. To lift it, put the variable in dockerd's own "
                      "environment, or build on a `docker-container` builder "
                      "created with --driver-opt env.BUILDKIT_STEP_LOG_MAX_SIZE=-1.")
            elif eff is None:
                print("  NOTE: could not read the buildx driver, so whether the "
                      "step-log limit is lifted is UNKNOWN — not assumed lifted.")
            try:
                build = sorted(missing_artefacts(root, refs_now, targets))
            except ReleaseMeasurementUnknown as exc:
                return _finish_unmeasured(result, a.json, exc)
            if build:
                print(f"  artefacts to build (absent, not merely moved): "
                      f"{', '.join(build)}")
            # BUILD THE TOOLS CONCURRENTLY. These were built one at a time,
            # so a round that touched yosys and OpenROAD paid for both end to
            # end even though nothing connects them — measured 2026-08-22:
            # OpenROAD alone ran 40+ minutes while every other artefact sat
            # idle. buildkit already parallelises WITHIN one target's DAG; what
            # was missing is that two targets never overlapped.
            #
            # Each tool keeps its OWN bake invocation rather than one bake over
            # many targets. That is what preserves per-tool attribution: an
            # exit code, a captured tail, a stall verdict and a retry category
            # that all belong to ONE tool. A single bake returns one number for
            # everything, and this morning's klayout failure was diagnosed in
            # minutes precisely because the failure named its target.
            #
            # Bounded, and low by default: each build runs its own `make -j`, so
            # the cap is about MEMORY, not cores. This host has 32 cores and a
            # yosys OOM took down two machines on 2026-08-19. GK_BUILD_PARALLEL
            # raises it for a host that can carry more.
            parallel = _build_parallelism(len(build))
            print(f"  building {len(build)} tool(s), {parallel} at a time: "
                  f"{', '.join(build)}", flush=True)

            def _build_one(t: str):
                want = artefact_tag(root, t, targets[t], refs_now)
                rc, _, err = _sh(["docker", "buildx", "bake", *bflag, "-f",
                                  str(root / "docker-bake.hcl"), "--push",
                                  "--progress", "plain",
                                  "--set", f"{t}.tags={want}", t], cwd=root,
                                 stream=True, env=BUILD_ENV,
                                 stall_grace=BUILD_STALL_GRACE_S,
                                 line_prefix=(f"[{t}] " if parallel > 1 else ""))
                return t, rc, err

            outcomes: dict = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=parallel) as pool:
                for t, rc, err in pool.map(_build_one, build):
                    outcomes[t] = (rc, err)

            # REPORTED IN THE ORDER THE ROUND ASKED FOR, not the order they
            # happened to finish. Two tools failing must produce the same
            # verdict every time, or a rerun disagrees with its own log.
            for t in build:
                rc, err = outcomes[t]
                if rc != 0:
                    build_category = (
                        "progress_stall" if watchdog.RC_STALLED == rc
                        else "build_failed"
                    )
                    print(f"[FAIL] {t} did not build; the release stops here so "
                          f"a broken tool is not tagged as a version:\n"
                          f"{err[-1500:]}", file=sys.stderr)
                    return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                        "stage": "tool_build", "category": build_category,
                        "retryable": (
                            _build_failure_category(err)
                            in _EXTENDED_RETRY_CATEGORIES),
                        "tool": t, "summary": _one_line_error(err),
                        "supervision": {
                            "mechanism": "activity_progress_lease",
                            "outcome": ("stalled" if rc == watchdog.RC_STALLED
                                        else "natural"),
                            "total_runtime_ceiling_seconds": None,
                        },
                    })
                result["built"].append(t)
            # COMPOSE THE IMAGE. Building the tool artefacts is not the
            # delivery — the image is what anyone runs, and a tool rebuilt
            # without recomposing is the same "merged but not shipping" failure
            # one hop further along. `eda-local` redirects each `FROM ${IMG_*}`
            # to the target just built, so the composed image cannot pull a
            # stale artefact from the registry.
            #
            # cocotb, pyuvm, sby and the ALIGN pair have no tool target at all —
            # the root Dockerfile installs them directly. For those, this compose
            # IS the whole rebuild, which is why it runs whenever anything moved
            # rather than only when a tool target was built.
            # Compose from the REGISTRY (`eda`), not from local targets
            # (`eda-local`). `eda` has no `contexts` block: every tool arrives as
            # the exact pinned artefact, so an unchanged tool is PULLED rather
            # than rebuilt, and the image is reproducible off this machine.
            # `eda-local` redirects each tool to a freshly built target — correct
            # for iterating on a tool, wrong for a daily release, because it
            # makes "only what changed was rebuilt" true only while the local
            # cache survives.
            #
            # THE VERSION IS DECIDED FIRST AND THE COMPOSE IS TAGGED WITH IT.
            # An earlier version composed `eda` — which tags `:${TAG}` (default
            # `dev`) and `:latest` — then smoked `vibeic-eda:local` and tagged
            # THAT as the new version. `:local` is `eda-local`'s tag, so it was
            # whatever the last local build left behind: the release would have
            # smoked one image and published a different, older one under the
            # new number. Caught by reading the bake file, not by any output —
            # every step succeeds.
            #
            # `--set` overrides the target's tags so the compose cannot move
            # `:latest` onto an image that has not passed the smoke yet.
            # Selected before every writer from a successfully measured local
            # inventory and an explicitly absent registry manifest.  Do not
            # re-read a possibly changed/failed local inventory here and do not
            # turn a registry UNKNOWN into permission to reuse a tag.
            assert selected_version is not None
            old, new = selected_version
            tag = f"ghcr.io/vibeic/vibeic-eda:{new}"
            result["version_candidate"] = {"from": old, "to": new,
                                           "tag": tag}
            print(f"  composing {tag} from the pinned artefacts …", flush=True)
            # `--progress plain` because the default TTY renderer emits almost
            # nothing to a redirected log. A compose that wedged after
            # `task-delete` was indistinguishable from one making quiet progress:
            # no CPU, no disk, no network, three lines of log, and the only way to
            # tell was reading dockerd's journal. A build that cannot be watched
            # cannot be diagnosed.
            # THE VERSION LABEL COMES FROM THE `TAG` VARIABLE, VIA THE ENV.
            #
            # `--set eda.tags=` replaces the tags without touching the bake
            # variable `TAG`, which stays at its default "dev" — so the labels
            # that reference ${TAG} would stamp every published image
            # `version=dev`. A different wrong answer than the 2026.06 this
            # replaced, but wrong the same way: a value nobody set on purpose.
            #
            # NOT `--set eda.labels.org.opencontainers.image.ref.name=`:
            # `--set` treats dots as path separators, so that spelling creates
            # a NEW label `ref-name` and leaves the real `ref.name` at "dev" —
            # measured. Setting TAG in the environment resolves tags and both
            # labels from one value, with no key that has to be spelled twice.
            bake = ["docker", "buildx", "bake", *bflag, "-f",
                    str(root / "docker-bake.hcl"), "--load", "--progress",
                    "plain", "--set", f"eda.tags={tag}", "eda"]
            rc, err, attempts = compose_with_retries(
                bake, root, extra_env={"TAG": tag.rsplit(":", 1)[-1]})
            result["compose_attempts"] = attempts
            if rc != 0:
                # NO LOCAL FALLBACK. There used to be one — compose `eda-local`,
                # which redirects each tool to a freshly built target — and it
                # stopped working the moment the artefact tag gained its recipe
                # component (#21): `eda-local`'s `contexts` map reconstructs tool
                # tags from the bake expression, which now produces
                # `eda-tool-iverilog:fe9dfab` while the root Dockerfile asks for
                # `eda-tool-openroad:92b079b-7444a2`. The keys no longer match the
                # FROM refs, so the redirect does not apply and `eda-local`
                # resolves from the registry — the same thing that just failed.
                #
                # A fallback that does not fall back is worse than none: it turns
                # one loud failure into two quiet ones and, when it did work, it
                # produced an image that could not be reproduced anywhere else.
                # A failed compose is a failed release, said once, loudly.
                last = attempts[-1] if attempts else {}
                print(f"[FAIL] the image did not compose after "
                      f"{len(attempts)} attempt(s); no "
                      f"version was cut, so nothing claims to be a release:"
                      f"\n{err[-6000:]}", file=sys.stderr)
                result["registry_composed"] = False
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "compose",
                    "category": last.get("category") or "compose_failed",
                    "retryable": (
                        last.get("category") in _EXTENDED_RETRY_CATEGORIES),
                    "attempts": len(attempts),
                    "summary": last.get("summary") or _one_line_error(err),
                })
            result["registry_composed"] = True

            # A LOCAL build can be runnable and still be impossible to PULL.
            # 0.3.0 composed, passed every tool smoke and pushed with 126 layers;
            # Docker's pull graph accepts at most 125. Check the exact candidate
            # BEFORE a version tag becomes durable in the registry. Failure to
            # measure is a refusal too: unknown depth is not a pullable image.
            depth_cmd = [
                "python3", str(root / "tools/check_image_depth.py"),
                "--compact", tag,
            ]
            # A 31 GB image can take as long as its measured byte stream needs.
            # This outer lease has no total-runtime ceiling; the checker also
            # supervises export/import by transferred bytes.
            drc, dout, derr = _sh(
                depth_cmd, cwd=root, progress=True,
                stall_grace=COMPACTION_STALL_GRACE_S,
            )
            if drc != 0:
                _sh(["docker", "rmi", tag])
                depth_detail = (derr or dout).strip()
                no_measurement = (
                    drc == watchdog.RC_STALLED
                    or "cannot prove image depth" in depth_detail
                )
                print(f"[{'REFUSE' if no_measurement else 'FAIL'}] composed image "
                      f"could not be made publishable; the tag was "
                      f"removed and NO version was cut:\n{(derr or dout).strip()}",
                      file=sys.stderr)
                return _finish(result, a.json, RC_NEEDS_HUMAN,
                               "unmeasured" if no_measurement else "failed", {
                    "stage": "image_depth", "category": (
                        "progress_stall" if drc == watchdog.RC_STALLED
                        else "image_depth_unmeasurable" if no_measurement
                        else "unpublishable_depth"),
                    "retryable": no_measurement,
                    "summary": _one_line_error(derr or dout),
                    "supervision": {
                        "mechanism": "transferred_bytes_progress_lease",
                        "outcome": ("stalled" if drc == watchdog.RC_STALLED
                                    else "unmeasured" if no_measurement
                                    else "natural"),
                        "total_runtime_ceiling_seconds": None,
                    },
                })
            print(f"  depth: {dout.strip()}")

            # SMOKE BEFORE THE VERSION IS REAL. A tag is a claim, and cutting one
            # over an image whose tools have never been started makes the claim
            # on nothing. The tag is removed on failure, so a bad image cannot be
            # left carrying a version number.
            bad = smoke_image(tag)
            result["smoke_failures"] = bad
            result["smoke"] = {
                "executed": True,
                "expected": len(SMOKE),
                "passed": len(SMOKE) - len(bad),
                "outcome": "natural_failure" if bad else "natural",
                "failures": bad,
            }
            if bad:
                _sh(["docker", "rmi", tag])
                unmeasured = [b for b in bad
                              if b.get("outcome", "natural_failure")
                              != "natural_failure"]
                if unmeasured:
                    print("[REFUSE] smoke produced NORECORD; a stalled or "
                          "unlaunchable probe says nothing about tool health. "
                          "The tag was removed and NO version was cut:",
                          file=sys.stderr)
                else:
                    print(f"[FAIL] {len(bad)} tool(s) do not run in the composed "
                          f"image; the tag was removed and NO version was cut:",
                          file=sys.stderr)
                for b in bad:
                    print(f"    {b['tool']} "
                          f"[{b.get('outcome', 'natural_failure')}]: "
                          f"{b['cmd']}\n      {b['err']}",
                          file=sys.stderr)
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "smoke", "category": (
                        "smoke_unmeasurable" if unmeasured else "tool_smoke"),
                    "retryable": False,
                    "summary": (
                        f"{len(unmeasured)} smoke probe(s) formed no record"
                        if unmeasured else f"{len(bad)} tool(s) failed smoke"),
                    "supervision": {
                        "mechanism": "container_activity_progress_lease",
                        "progress_basis": ["output_bytes",
                                           "container_process_tree_cpu"],
                        "outcome": (unmeasured[0].get("outcome")
                                    if unmeasured else "natural"),
                        "total_runtime_ceiling_seconds": None,
                    },
                })
            print(f"  smoke: all {len(SMOKE)} tools run in {tag}")

            # Close the selection/push race.  `docker manifest inspect` asks the
            # registry even though a local image now has the same name.  Only a
            # second explicit not-found answer permits the immutable tag to be
            # created; a concurrent publisher or an unmeasured registry state
            # cannot be overwritten by `docker push`.
            try:
                before_push = _require_measured(
                    _probe_remote_manifest(tag), stage="pre_push_registry_guard",
                    category="candidate_tag_state_unmeasurable",
                )
            except ReleaseMeasurementUnknown as exc:
                _sh(["docker", "rmi", tag])
                return _finish_unmeasured(result, a.json, exc)
            if before_push.state is Presence.PRESENT:
                _sh(["docker", "rmi", tag])
                print(f"[REFUSED] {tag} appeared in the registry after version "
                      "selection; the local candidate was removed and the "
                      "immutable tag was not overwritten", file=sys.stderr)
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "pre_push_registry_guard",
                    "category": "candidate_tag_collision",
                    "retryable": True,
                    "tag": tag,
                    "summary": "candidate tag already exists in the registry",
                })

            prc, _, perr = _sh(
                ["docker", "push", tag], progress=True,
                stall_grace=PUSH_STALL_GRACE_S,
            )
            pushed = prc == 0
            if not pushed:
                print(f"  PUSH FAILED — {new} exists here and nowhere else:"
                      f"\n{perr[-800:]}", file=sys.stderr)
            result["pushed"] = pushed
            result["version"] = {"from": old, "to": new}

            if pushed:
                # A zero push rc is not publication authority.  Freeze the
                # exact remote immutable-manifest identity before changing the
                # Git record or the mutable latest tag.  If this readback is
                # absent or unmeasurable, the sidecar records that the command
                # returned zero but no later writer is allowed to run.
                try:
                    immutable_ok, immutable_evidence = \
                        verify_remote_immutable_publication(new)
                except ReleaseMeasurementUnknown as exc:
                    result["immutable_publication"] = {
                        "verified": False, "unmeasured": exc.as_dict(),
                    }
                    return _finish_unmeasured(result, a.json, exc)
                result["immutable_publication"] = immutable_evidence
                if not immutable_ok:
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "immutable_publication",
                            "category": immutable_evidence.get(
                                "category", "immutable_readback_failed"),
                            "retryable": immutable_evidence.get(
                                "retryable", True),
                            "summary": immutable_evidence.get(
                                "summary", "immutable tag readback failed"),
                        })
                immutable_identity = immutable_evidence["identity"]

                # The immutable version tag is now remotely proven.  Publish
                # its Git authority before moving :latest, so a record push or
                # readback failure leaves the old mutable tag untouched and a
                # later noop has one unambiguous recovery order.
                (root / "VERSION").write_text(new + "\n", encoding="utf-8")
                # RECOMPUTE. `fp` above was measured before `rewrite_pin` and
                # `retag_images` edited the root Dockerfile, and those edits feed
                # `compose_recipe_hash`. Recording the pre-edit value made the
                # record IRREPRODUCIBLE: the shipped tree hashes to something
                # else, so every later run reads "this pin set has never been
                # released" and cuts another version. Measured on 0.2.45 — the
                # released tree computes e4e0a5f6 while the file it shipped
                # records 94d85fda, and the next run started composing 0.2.46
                # with nothing whatsoever changed.
                #
                # This is the program's own stated refusal — "it will not cut an
                # image version when no tool changed" — defeated by the order of
                # two writes. The decision fingerprint (early) and the RECORDED
                # fingerprint (here) answer different questions and only the
                # second has to match the tree that shipped.
                write_released_record(
                    root, new, targets,
                    manifest_identity=immutable_identity,
                )
                # THIS REPO'S OWN DOCS, BEFORE THE RECORD COMMIT. VERSION is
                # written directly above, which is why README.md's five
                # `docker pull` / `docker run` commands sat at 0.2.56 while
                # VERSION said 0.2.63 — seven releases telling a reader to pull
                # an image the release had superseded.
                _rc, _dout, _derr = _sh(
                    ["python3", str(root / "sync_image_version.py"),
                     "--set", new], cwd=str(root))
                print(f"  docs: {'synced to ' + new if _rc == 0 else 'SYNC FAILED — ' + (_dout or '').strip()[-160:]}")
                _ok, _note = commit_release_record(root, new, pin_files)
                result["release_record_publication"] = {
                    "committed": bool(_ok), "commit_note": _note,
                }
                print(f"  record: {_note}")
                if not _ok:
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "release_record_commit",
                            "category": "release_record_commit_failed",
                            "retryable": True,
                            "summary": _note,
                        })

                # `commit_release_record` historically attempted this push but
                # returned only whether the commit succeeded. Measure origin,
                # retry an explicit absence, then measure it again.
                try:
                    record_ok, record_evidence = \
                        ensure_release_record_published(root)
                except ReleaseMeasurementUnknown as exc:
                    return _finish_unmeasured(result, a.json, exc)
                result["release_record_publication"].update(record_evidence)
                if not record_ok:
                    note = (record_evidence.get("repair", {}).get("note")
                            or record_evidence.get("final_detail")
                            or "release record did not reach origin")
                    print(f"  RECORD PUSH FAILED — {note}", file=sys.stderr)
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "release_record_push",
                            "category": "release_record_push_failed",
                            "retryable": True,
                            "summary": note,
                        })

                # Re-read both authorities immediately before the only mutable
                # registry write.  The record must still be current and the
                # immutable tag must still have the identity frozen above.
                try:
                    authority_ok, authority_evidence = \
                        authorize_latest_repair(root, new)
                except ReleaseMeasurementUnknown as exc:
                    return _finish_unmeasured(result, a.json, exc)
                result["latest_authority"] = authority_evidence
                if not authority_ok:
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "latest_authority",
                            "category": "release_authority_changed",
                            "retryable": True,
                            "summary": authority_evidence.get(
                                "summary", "release authority changed"),
                        })
                try:
                    still_ok, still_evidence = \
                        verify_remote_immutable_publication(new)
                except ReleaseMeasurementUnknown as exc:
                    return _finish_unmeasured(result, a.json, exc)
                result["immutable_publication"]["pre_latest"] = still_evidence
                if (not still_ok
                        or still_evidence.get("identity") != immutable_identity):
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "latest_authority",
                            "category": "immutable_manifest_changed",
                            "retryable": True,
                            "summary": (f"{tag} changed after its authoritative "
                                        "readback; :latest was not moved"),
                        })

                # Only now may :latest move.  A failed latest publication leaves
                # a complete immutable+record authority for the next noop to
                # repair; it never leaves latest ahead of the Git record.
                latest_ref = f"{RELEASE_IMAGE}:latest"
                ltrc, ltout, lterr = _sh(["docker", "tag", tag, latest_ref])
                latest_pushed = False
                latest_error = ""
                if ltrc != 0:
                    latest_error = _one_line_error(lterr or ltout)
                else:
                    lprc, lpout, lperr = _sh(
                        ["docker", "push", latest_ref], progress=True,
                        stall_grace=PUSH_STALL_GRACE_S,
                    )
                    latest_pushed = lprc == 0
                    if not latest_pushed:
                        latest_error = _one_line_error(lperr or lpout)
                result["latest_publication"] = {
                    "ref": latest_ref,
                    "tagged": ltrc == 0,
                    "pushed": latest_pushed,
                }
                if latest_error:
                    result["latest_publication"]["error"] = latest_error
                    print(f"  LATEST PUSH FAILED — {latest_ref} was not moved "
                          f"to {tag}: {latest_error}", file=sys.stderr)
                if not latest_pushed:
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "latest_push",
                            "category": "latest_push_failed",
                            "retryable": True,
                            "ref": latest_ref,
                            "summary": latest_error or (
                                "latest tag was not published"),
                        })

                # Read both names back, then bind the result to the frozen
                # immutable identity rather than merely comparing two names
                # which might both have changed.
                try:
                    remote_ok, remote_evidence = \
                        verify_remote_release_publication(new)
                except ReleaseMeasurementUnknown as exc:
                    return _finish_unmeasured(result, a.json, exc)
                result["remote_publication"] = remote_evidence
                final_identity = (remote_evidence.get("manifests", {})
                                  .get("version", {}).get("identity"))
                if remote_ok and final_identity != immutable_identity:
                    remote_ok = False
                    remote_evidence.update({
                        "category": "immutable_manifest_changed",
                        "summary": (f"{tag} no longer has the identity proven "
                                    "immediately after its push"),
                    })
                if not remote_ok:
                    return _finish(
                        result, a.json, RC_NEEDS_HUMAN, "failed", {
                            "stage": "publication_readback",
                            "category": remote_evidence.get(
                                "category", "publication_readback_failed"),
                            "retryable": True,
                            "summary": remote_evidence.get(
                                "summary", "remote publication readback failed"),
                        })
            print(f"  VERSION {old} -> {new}  "
                  f"{'published' if pushed else 'LOCAL ONLY'}")
            # PUBLISHING AN IMAGE IS NOT VERIFYING A PLUGIN AGAINST IT.
            #
            # vibe-ic#754 tied these together: publish, then move vibe-ic's
            # anchor, because `:latest` kept drifting off the pinned version.
            # The tie was wrong in a way the code stated out loud without
            # anyone reading it back. vibe-ic pins the image because — its own
            # comment — the tag "matches what the plugin was VERIFIED AGAINST".
            # But the only thing this round establishes before anchoring is
            # that the tag is PULLABLE. Nothing here runs vibe-ic's suite
            # against the new image. So the anchor asserted a verification that
            # had not happened, and it asserted it on every single release.
            #
            # Two repos, two facts, two owners:
            #   "0.3.16 exists"                      — this repo's, settled here.
            #   "the plugin is verified against it"  — vibe-ic's, and only
            #                                          vibe-ic can settle it, by
            #                                          actually running its suite.
            #
            # So a release no longer checks anything into vibe-ic. The anchor
            # moves when vibe-ic re-verifies, on vibe-ic's own schedule, and the
            # sentence in its source becomes true again. This also ends the loop
            # where the anchor PR opened by a round made
            # `fork_github_convergence` non-zero and skipped the NEXT round's
            # certified publish — measured across five rounds, 2026-08-20.

            # Image publication is not completion of the whole round. Keep
            # the public snapshot unchanged until run_0530.sh certifies its
            # post-release evidence (owner directive 2026-10-07).
            if pushed:
                _pok, _pnote = publish_release_page(new)
                _pstate = ("deferred" if _pok is None else
                           "published" if _pok else "FAILED")
                print(f"  page: {_pstate} — {_pnote}")
                result.setdefault("page_publish", {})["status"] = _pstate
                result.setdefault("page_publish", {})["ok"] = bool(_pok)
                result["page_publish"]["note"] = _pnote

    if not moved and not absent and not unreleased:
        # A matching LOCAL fingerprint is necessary, not sufficient.  The old
        # noop accepted it without asking whether the immutable version still
        # had a registry manifest, whether latest still named that manifest, or
        # whether the commit carrying RELEASED.json ever left this host.
        # First establish that the immutable version exists.  Do not publish a
        # git release record for a missing/unmeasurable image.  A stale/missing
        # mutable latest is repairable, but its repair authority is the git
        # record, so converge that record before performing the mutable write.
        try:
            remote_ok, remote_evidence = repair_latest_publication(
                rec.get("version"), repair=False,
            )
        except ReleaseMeasurementUnknown as exc:
            return _finish_unmeasured(result, a.json, exc)
        result["remote_publication"] = remote_evidence
        recorded_identity = rec.get("manifest_identity")
        remote_version_identity = (remote_evidence.get("manifests", {})
                                   .get("version", {}).get("identity"))
        if recorded_identity is not None:
            if not (isinstance(recorded_identity, str)
                    and re.fullmatch(r"[0-9a-f]{64}", recorded_identity)):
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "noop_publication",
                    "category": "recorded_manifest_identity_invalid",
                    "retryable": False,
                    "summary": ("RELEASED.json manifest_identity is not an "
                                "exact canonical manifest identity"),
                })
            if remote_version_identity != recorded_identity:
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "noop_publication",
                    "category": "recorded_manifest_identity_mismatch",
                    "retryable": False,
                    "summary": (f"{RELEASE_IMAGE}:{rec.get('version')} no "
                                "longer has the identity recorded when it was "
                                "published"),
                })
        latest_expected_identity = recorded_identity or remote_version_identity
        repairable_latest = remote_evidence.get("category") in {
            "latest_manifest_missing", "latest_manifest_mismatch",
        }
        if not remote_ok and not repairable_latest:
            summary = (remote_evidence.get("repair", {}).get("error")
                       or remote_evidence.get("summary")
                       or "remote version/latest publication is incomplete")
            print(f"[FAIL] noop publication check: {summary}", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "noop_publication",
                "category": remote_evidence.get(
                    "category", "latest_repair_failed"),
                "retryable": remote_evidence.get("retryable", True),
                "summary": summary,
            })

        # A previous round may have proved and pushed the immutable tag, then
        # failed while committing its local VERSION/RELEASED update.  Merely
        # calling the record *push* helper in that state pushes old HEAD forever.
        # Recover the missing commit first, under the same immutable identity
        # already measured above, and only through the closed pin-only admission.
        if not a.dry_run:
            immutable_identity = (remote_evidence.get("manifests", {})
                                  .get("version", {}).get("identity"))
            try:
                recovery_ok, recovery_evidence = \
                    recover_uncommitted_release_record(
                        root, rec.get("version"), targets,
                        fingerprint=fp,
                        immutable_identity=immutable_identity,
                    )
            except ReleaseMeasurementUnknown as exc:
                return _finish_unmeasured(result, a.json, exc)
            result["release_record_recovery"] = recovery_evidence
            if not recovery_ok:
                summary = recovery_evidence.get(
                    "summary", "uncommitted release record was not recoverable")
                print(f"[FAIL] noop release-record recovery: {summary}",
                      file=sys.stderr)
                return _finish(
                    result, a.json, RC_NEEDS_HUMAN, "failed", {
                        "stage": "release_record_recovery",
                        "category": recovery_evidence.get(
                            "category", "release_record_recovery_failed"),
                        "retryable": True,
                        "summary": summary,
                    })

        try:
            record_ok, record_evidence = ensure_release_record_published(
                root, repair=not a.dry_run,
            )
        except ReleaseMeasurementUnknown as exc:
            return _finish_unmeasured(result, a.json, exc)
        result["release_record_publication"] = record_evidence
        if not record_ok:
            note = (record_evidence.get("repair", {}).get("note")
                    or record_evidence.get("final_detail")
                    or "release record did not reach origin")
            print(f"[FAIL] noop could not repair release-record publication: "
                  f"{note}", file=sys.stderr)
            return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                "stage": "release_record_push",
                "category": "release_record_push_failed",
                "retryable": True,
                "summary": note,
            })

        if not remote_ok:
            if a.dry_run:
                summary = (remote_evidence.get("summary")
                           or "latest publication requires repair")
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "noop_publication",
                    "category": remote_evidence.get(
                        "category", "latest_repair_deferred"),
                    "retryable": True,
                    "summary": summary,
                })
            try:
                remote_ok, remote_evidence = repair_latest_publication(
                    rec.get("version"), repair=True,
                    expected_identity=latest_expected_identity,
                    authorize=(
                        lambda version: authorize_latest_repair(root, version)
                    ),
                )
            except ReleaseMeasurementUnknown as exc:
                return _finish_unmeasured(result, a.json, exc)
            result["remote_publication"] = remote_evidence
            if not remote_ok:
                summary = (remote_evidence.get("repair", {}).get("error")
                           or remote_evidence.get("summary")
                           or "remote version/latest publication is incomplete")
                print(f"[FAIL] noop publication check: {summary}",
                      file=sys.stderr)
                return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
                    "stage": "noop_publication",
                    "category": remote_evidence.get(
                        "category", "latest_repair_failed"),
                    "retryable": remote_evidence.get("retryable", True),
                    "summary": summary,
                })

        # A NOOP NO LONGER RETRIES THE ANCHOR, because a release no longer
        # sets it. "Operationally converged" used to include vibe-ic's anchor
        # being current — a condition this repo cannot honestly settle: the
        # anchor means "the plugin was verified against this image", and
        # nothing here verifies that. Holding a release open until another
        # repo records a claim we never established made the release fail for
        # a fact that was not ours to assert. See the note at the publish site.

        print("[PASS] every pin is its fork's tip, every pinned artefact exists, "
              "the recorded version and :latest resolve to the same remote "
              "manifest, and the release record is published — nothing to do")
        return _finish(result, a.json, RC_OK, "noop")
    elif a.dry_run:
        # A release follows from ANY of the three, not from `unreleased` alone —
        # an absent artefact means the image cannot already contain it.
        print(f"[DRY RUN] would move {len(moved)} pin(s), build {len(absent)} "
              f"absent artefact(s), and "
              f"{'cut a release' if (moved or absent or unreleased) else 'cut no release'}")
        return _finish(result, a.json, RC_OK, "dry_run")
    if result.get("pushed") is False:
        return _finish(result, a.json, RC_NEEDS_HUMAN, "failed", {
            "stage": "push", "category": "push_failed",
            "retryable": True,
            "summary": "candidate image exists only on the build host",
        })
    status = "published" if result.get("pushed") else "completed"
    return _finish(result, a.json, RC_OK, status)


def _option_value(argv: List[str], name: str) -> Optional[str]:
    """Read one argparse-style string option for pre-parser failure evidence."""
    for idx, item in enumerate(argv):
        if item == name:
            return argv[idx + 1] if idx + 1 < len(argv) else None
        prefix = name + "="
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _last_line(proc) -> str:
    return ((proc.stdout or proc.stderr or "").strip().splitlines()
            or ["(no output)"])[-1]


def publish_release_page(version: str, *, run=None,
                         out: Optional[str] = None) -> Tuple[Optional[bool], str]:
    """Defer the public page; optionally render an explicit isolated preview.

    A published image does not yet have the outer round's LL/current evidence.
    Production output is therefore deferred BEFORE discovery or rendering,
    regardless of the caller's environment. Only run_0530's certified tail
    publishes it. None means deferred, not a page PASS or a release failure.
    Preview results remain advisory to an image already in the registry.
    """
    import gk_state  # noqa: PLC0415

    if not out or gk_state.is_production_path(out):
        return None, ("deferred until run_0530.sh validates complete "
                      "current-round evidence")
    # Resolved at CALL time, never bound as a default argument. A default
    # `run=subprocess.run` captured the real function at import, so every
    # test that stubs `daily_release.subprocess.run` (the release tests do,
    # module-wide) still ran the real discover_forks with
    # GK_PRODUCTION_WRITER=1 -- measured 2026-09-27 11:52: a pytest run of
    # #259 rewrote the production ledger from a scratch checkout.
    run = run or subprocess.run
    here = Path(__file__).resolve()
    env = {**os.environ, "GK_PRODUCTION_WRITER": "1"}
    notes = []
    try:
        disc = run([sys.executable, str(here.with_name("discover_forks.py"))],
                   env=env, capture_output=True, text=True, timeout=1800)
        if disc.returncode != 0:
            notes.append(f"ledger refresh rc={disc.returncode}: "
                         f"{_last_line(disc)}")
        pg = run([sys.executable, str(here.with_name("build_page.py")),
                  "--out", out,
                  "--expect-image-version", version],
                 env=env, capture_output=True, text=True, timeout=600)
        ok = pg.returncode == 0
        notes.append(_last_line(pg))
    except Exception as e:      # noqa: BLE001
        ok = False
        notes.append(f"{e.__class__.__name__}: {e}")
    return ok, " · ".join(notes)


def main(argv=None) -> int:
    """Serialize the complete measurement/mutation/publication transaction.

    A lock around only ``imagetools create`` is too small: an older noop can
    measure stale authority, pause while a new release publishes, then roll
    ``:latest`` back and validate its own write.  Every supported entry point
    therefore acquires the same host-wide lock before it measures anything and
    holds it through final readback and sidecar publication.  Contention is a
    named fail-closed result; it never waits and never runs the inner program.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    json_path = _option_value(args, "--json")
    started = _utcnow()
    lock_path = _release_lock_path()
    try:
        lock_fd = _acquire_release_lock(lock_path)
    except BlockingIOError:
        result = {
            "program": "daily_release",
            "schema_version": 2,
            "status": "blocked",
            "started_at": started,
            "finished_at": _utcnow(),
            "exit_code": RC_NEEDS_HUMAN,
            "failure": {
                "stage": "release_lock",
                "category": "release_already_running",
                "retryable": True,
                "summary": f"another release owns {lock_path}",
            },
        }
        if json_path:
            _atomic_json(json_path, result)
        print(f"[BLOCKED] another release owns {lock_path}; no measurement "
              "or mutation was attempted", file=sys.stderr)
        return RC_NEEDS_HUMAN
    except OSError as exc:
        result = {
            "program": "daily_release",
            "schema_version": 2,
            "status": "unmeasured",
            "started_at": started,
            "finished_at": _utcnow(),
            "exit_code": RC_NEEDS_HUMAN,
            "failure": {
                "stage": "release_lock",
                "category": "release_lock_unmeasurable",
                "retryable": False,
                "summary": f"could not establish release lock {lock_path}: {exc}",
            },
        }
        if json_path:
            _atomic_json(json_path, result)
        print(f"[UNMEASURED] could not establish release lock {lock_path}: "
              f"{exc}", file=sys.stderr)
        return RC_NEEDS_HUMAN

    try:
        return _main_under_release_lock(args)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
