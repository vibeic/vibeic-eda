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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_pins_current import (branch_is_ours, check_one,       # noqa: E402
                                pin_sites,
                                pinned_refs)
import progress_watchdog as watchdog                            # noqa: E402
from capability_smoke import run_container_probe               # noqa: E402

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: How many hex characters the artefact tag uses. `short()` in docker-bake.hcl.
SHORT = 7


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


class ReleaseMeasurementUnknown(RuntimeError):
    """A release input could not be measured, so mutation must not proceed."""

    def __init__(self, *, stage: str, category: str, subject: str,
                 detail: str):
        self.stage = stage
        self.category = category
        self.subject = subject
        self.detail = detail
        super().__init__(f"{subject}: {detail}")

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "category": self.category,
            "subject": self.subject,
            "detail": self.detail,
        }


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
)
_EXTENDED_RETRY_CATEGORIES = {
    "dns", "network_timeout", "network_reset", "service_unavailable",
}


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
                bake, cwd=root, stream=True, env=BUILD_ENV,
                stall_grace=BUILD_STALL_GRACE_S,
            )
        else:
            rc, _, err = run(
                bake, cwd=root, stream=True, env=BUILD_ENV,
                stall_grace=BUILD_STALL_GRACE_S,
            )
        category = transient_failure_category(err)
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


def _sh(cmd, cwd=None, timeout=None, stream=False, env=None,
        stall_grace=None, progress=False):
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

            def pump() -> None:
                nonlocal tail_size, progress_bytes
                assert proc.stdout is not None
                while True:
                    chunk = os.read(proc.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    progress_bytes += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
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
            r = subprocess.run(["git", "-C", str(clone), *a],
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


def pins_fingerprint(pins: Dict[str, str]) -> str:
    """A stable digest of the whole pin set — what a released image was built from."""
    blob = ";".join(f"{k}={v}" for k, v in sorted(pins.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def released_record(eda_root: Path) -> dict:
    f = eda_root / "RELEASED.json"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text())
    except ValueError:
        return {}


def write_released_record(eda_root: Path, version: str,
                          targets: Dict[str, List[str]]) -> dict:
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
    rec = {
        "_comment": "What the last PUBLISHED image was built from. A pin set "
                    "not matching this has never been released, however current "
                    "the pins look. The fingerprint is measured AFTER every edit "
                    "this run made, so a later run can reproduce it from the "
                    "tree that shipped.",
        "version": version,
        "pins_fingerprint": pins_fingerprint({
            **pinned_refs(eda_root),
            **{f"recipe:{k}": recipe_hash(eda_root, k) for k in targets},
            "recipe:__compose__": compose_recipe_hash(eda_root)}),
        "pins": {k: v for k, v in sorted(pinned_refs(eda_root).items())},
    }
    (eda_root / "RELEASED.json").write_text(
        json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return rec


#: A changed line in a pin file that is a PIN and nothing else. Anything a
#: release run writes into these files matches one of these; anything that does
#: not is a human's edit and is refused rather than swept into a release commit.
_PIN_LINE_RE = re.compile(
    r"^\s*(?:"
    r"ARG\s+[A-Z0-9_]+_REF=[0-9a-f]{40}"                      # tools/*/Dockerfile
    r"|ARG\s+IMG_[A-Z0-9_]+=\S+"                              # root Dockerfile tag
    r"|variable\s+\"[A-Z0-9_]+_(?:REF|RECIPE)\"\s*\{.*\}"     # docker-bake.hcl
    r")")


def _diff_is_only_pins(eda_root: Path, rel: str) -> Tuple[bool, str]:
    """Does `rel`'s working-tree diff contain pin lines and nothing else?

    The question this answers is "is every change here one THIS PROGRAM makes",
    not "did this program make them" — a stricter reading would need to diff
    against a snapshot, and the looser one is what the refusal is for: keeping a
    human's half-finished edit out of an automated release commit.
    """
    rc, out, err = _sh(["git", "-C", str(eda_root), "diff", "-U0", "--", rel],
                       timeout=60)
    if rc != 0:
        return False, f"git diff failed: {(err or out).strip()[:60]}"
    for line in out.splitlines():
        if line[:1] not in ("+", "-") or line[:3] in ("+++", "---"):
            continue
        if not _PIN_LINE_RE.match(line[1:]):
            return False, f"non-pin change: {line[1:].strip()[:48]}"
    return True, ""


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

    Does not push: whether the record reaches origin is the caller's call, and a
    failed push must not look like a failed release. Returns (committed, note)
    and never raises — a release that published successfully must not be
    reported as failed because a commit could not be made.
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
            continue
        ok, why = _diff_is_only_pins(eda_root, rel)
        (admitted if ok else refused).append(rel if ok else f"{rel} ({why})")
    present += admitted
    rc, out, err = _sh(["git", "-C", str(eda_root), "status", "--porcelain",
                        "--"] + present, timeout=60)
    if rc != 0:
        return False, f"git status failed: {(err or out).strip()[:120]}"
    if not out.strip():
        return False, "record already matches HEAD — nothing to commit"
    rc, out, err = _sh(["git", "-C", str(eda_root), "add", "--"] + present,
                       timeout=60)
    if rc != 0:
        return False, f"git add failed: {(err or out).strip()[:120]}"
    msg = (f"release: record {version} as published\n"
           f"\n"
           f"Written by daily_release after the image was pushed. The publish is "
           f"durable (it is in a registry); this is the half that was not "
           f"(vibeic-eda#71).\n")
    if admitted:
        msg += ("\nThe pins this run moved are committed here too, so a fresh "
                "clone rebuilds what this record says shipped:\n"
                + "".join(f"  {f}\n" for f in admitted))
    if refused:
        msg += ("\nNOT committed — carries changes beyond the pin lines, which "
                "are someone else's to review:\n"
                + "".join(f"  {f}\n" for f in refused))
    rc, out, err = _sh(["git", "-C", str(eda_root), "commit", "-m", msg,
                        "--"] + present, timeout=120)
    if rc != 0:
        return False, f"git commit failed: {(err or out).strip()[:160]}"
    note = f"recorded {version} in a commit"
    if admitted:
        note += f" with {len(admitted)} pin file(s)"
    if refused:
        note += (f"; {len(refused)} pin file(s) REFUSED, still uncommitted: "
                 + ", ".join(refused))

    _pushed, _why = push_release_record(eda_root)
    note += f"; {_why}"
    return True, note


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
    rc, out, err = _sh(["git", "-C", str(eda_root), "fetch", "--progress",
                        "origin", branch], progress=True,
                       stall_grace=PUSH_STALL_GRACE_S)
    if rc != 0:
        kind = "stalled; state UNKNOWN" if rc == watchdog.RC_STALLED else "failed"
        return False, f"NOT PUSHED (fetch {kind}: {(err or out).strip()[:100]})"

    rc, _, _ = _sh(["git", "-C", str(eda_root), "merge-base", "--is-ancestor",
                    f"origin/{branch}", "HEAD"])
    if rc != 0:
        return False, (f"NOT PUSHED — origin/{branch} is not an ancestor of HEAD; "
                       f"the remote moved while the image was building. The "
                       f"record is committed; land it by hand.")

    rc, out, err = _sh(["git", "-C", str(eda_root), "push", "--progress",
                        "origin", branch], progress=True,
                       stall_grace=PUSH_STALL_GRACE_S)
    if rc != 0:
        kind = "stalled; state UNKNOWN" if rc == watchdog.RC_STALLED else "failed"
        return False, f"NOT PUSHED (push {kind}: {(err or out).strip()[:200]})"
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


def _probe_remote_manifest(ref: str) -> PresenceMeasurement:
    """Measure a registry tag; only an explicit manifest absence is ABSENT."""
    rc, out, err = _sh(["docker", "manifest", "inspect", ref], timeout=180)
    detail = (err or out).strip()
    if rc == 0:
        return PresenceMeasurement(Presence.PRESENT, ref, "registry", detail)
    if _REMOTE_ABSENCE.search(detail):
        return PresenceMeasurement(Presence.ABSENT, ref, "registry", detail)
    return PresenceMeasurement(Presence.UNKNOWN, ref, "registry", detail or (
        f"docker manifest inspect exited {rc} without an explicit not-found answer"
    ))


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
    """Digest of the Dockerfile that BUILDS this tool.

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
    return hashlib.sha256(f.read_bytes()).hexdigest()[:6]


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true",
                    help="move pins and stop; do not build or version")
    ap.add_argument("--json", default=None)
    ap.add_argument("--smoke-only", default=None, metavar="IMAGE",
                    help="run the tool smoke against an existing image and stop")
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
    }
    # Replace yesterday's terminal record before doing anything that can return
    # early.  A crash can now leave "running" (incomplete and visibly so), never
    # a prior day's success masquerading as this run's answer.
    if a.json:
        _atomic_json(a.json, result)

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
    rec = released_record(root)
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

    # Publish the recipe digests only after every pre-mutation measurement has
    # formed a record.  `artefact_tag` hashes the source Dockerfile directly, so
    # the registry questions above do not depend on this write.
    stale = write_recipe_vars(root, targets)
    for s in stale:
        print(f"  recipe changed: {s}")

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
        pin_files: List[str] = [f for m in moved for f in (m.get("files") or [])]

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
            for t in build:
                print(f"  building {t} …", flush=True)
                # `--push` is what makes the next hop incremental FOR ANYONE
                # ELSE. Without it the artefact exists on one machine, and the
                # release image can only be composed here, from this cache — a
                # per-tool architecture whose incrementality is an accident of
                # local state. Measured 2026-07-29: all 8 artefacts existed
                # locally and ghcr held none of them.
                # `--set` because the bake file's own tag expression knows only
                # the source commit. Overriding here keeps ONE definition of an
                # artefact's identity — this function's — rather than two that
                # can disagree.
                # refs_NOW, not refs_all: refs_all is the pre-move reading, so
                # a run that moved a pin would build the artefact and tag it with
                # the commit it replaced.
                want = artefact_tag(root, t, targets[t], refs_now)
                # `--progress plain` here too. I added it to the compose after a
                # wedged build was indistinguishable from a quiet one, and left
                # the TOOL builds — the LONGER of the two — still writing nothing
                # to the log. Fixing one instance of a defect and not its sibling
                # is how the sibling gets found the hard way.
                rc, _, err = _sh(["docker", "buildx", "bake", *bflag, "-f",
                                  str(root / "docker-bake.hcl"), "--push",
                                  "--progress", "plain",
                                 "--set", f"{t}.tags={want}", t], cwd=root,
                                 stream=True, env=BUILD_ENV,
                                 stall_grace=BUILD_STALL_GRACE_S)
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
                            transient_failure_category(err)
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
            bake = ["docker", "buildx", "bake", *bflag, "-f",
                    str(root / "docker-bake.hcl"), "--load", "--progress",
                    "plain", "--set", f"eda.tags={tag}", "eda"]
            rc, err, attempts = compose_with_retries(bake, root)
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
                # `latest` moves only after a smoke pass AND a successful push,
                # so it can never point at something nobody could pull.
                _sh(["docker", "tag", tag, "ghcr.io/vibeic/vibeic-eda:latest"])
                _sh(["docker", "push", "ghcr.io/vibeic/vibeic-eda:latest"],
                    progress=True, stall_grace=PUSH_STALL_GRACE_S)
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
                write_released_record(root, new, targets)
                # #71 — and COMMIT it. Only when the image was actually pushed:
                # a LOCAL ONLY build has published nothing, so recording it as
                # released would assert a release nobody can pull.
                if pushed:
                    # THIS REPO'S OWN DOCS, BEFORE THE RECORD COMMIT. VERSION is
                    # written directly above, which is why README.md's five
                    # `docker pull` / `docker run` commands sat at 0.2.56 while
                    # VERSION said 0.2.63 — seven releases telling a reader to pull
                    # an image the release had superseded.
                    # `sync_image_version.py` is the tool that knows where those
                    # pointers are, and writing VERSION without it is what let them
                    # drift. It runs AFTER the push because it refuses to point at a
                    # tag `docker pull` cannot resolve, and BEFORE the record commit
                    # so README.md lands in the same commit as VERSION rather than
                    # as a stray edit nobody stages.
                    _rc, _dout, _derr = _sh(
                        ["python3", str(root / "sync_image_version.py"),
                         "--set", new], cwd=str(root))
                    print(f"  docs: {'synced to ' + new if _rc == 0 else 'SYNC FAILED — ' + (_dout or '').strip()[-160:]}")
                    _ok, _note = commit_release_record(root, new, pin_files)
                    print(f"  record: {_note}")
            print(f"  VERSION {old} -> {new}  "
                  f"{'published' if pushed else 'LOCAL ONLY'}")
            # vibe-ic#754 — PUBLISHING AND ANCHORING ARE ONE ACTION.
            #
            # They were two, with nothing linking them, so `:latest` moved off the
            # version vibe-ic pins on every single release and the two were reunited
            # only when a landing gate happened to look. Applied by hand four times
            # before this. Only on a real publish: a LOCAL ONLY build has nothing to
            # anchor TO, and pointing the repo at an unpullable tag is worse than
            # leaving it behind a real one.
            #
            # Advisory by construction — it opens a PR, it does not push to main, and
            # a failure here must not fail a release that already succeeded. The
            # landing gate that used to be the only line of defence is still there and
            # is still authoritative; this just stops it being the FIRST time anyone
            # notices.
            if pushed:
                try:
                    import pr_notify as _prn
                    _aok, _anote = _prn.open_anchor_pr(new)
                except Exception as _e:      # noqa: BLE001
                    _aok, _anote = False, f"{_e.__class__.__name__}: {_e}"
                print(f"  anchor: {_anote}")
                result.setdefault("anchor_pr", {})["ok"] = bool(_aok)
                result["anchor_pr"]["note"] = _anote

    if not moved and not absent and not unreleased:
        print("[PASS] every pin is its fork's tip, every pinned artefact exists, "
              "and this pin set is already released — nothing to do")
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


if __name__ == "__main__":
    sys.exit(main())
