#!/usr/bin/env python3
"""Supervise long-running gate commands by forward progress, not runtime.

This is the vibeic-eda port of vibe-ic's plugin-wide watchdog.  A command may
run for as long as it needs while any trusted progress signal advances.  It is
stopped only when *all* configured signals stay flat for ``stall_grace_s``.

The generic signals are monotonic output bytes, an optional external/domain
token, and CPU consumed by the host process tree.  A temporarily unavailable
probe carries its previous value forward; losing a reading is never progress.
The stall outcome has its own return code so callers cannot report it as a test
failure or as a fixed-duration timeout.

Short network/API probes should continue to use bounded request deadlines.
This module is for long work whose runtime is not itself a verdict.
"""
from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple


RC_STALLED = 199
DEFAULT_POLL_S = 5.0
DEFAULT_STALL_GRACE_S = 1800.0


@dataclass(frozen=True)
class SupervisedResult:
    """Result of one progress-supervised command."""

    rc: int
    out: str
    err: str
    outcome: str  # natural | stalled | launch_error
    elapsed_s: float

    @property
    def stalled(self) -> bool:
        return self.outcome == "stalled"


class ProgressMeter:
    """Fuse independent progress signals into one monotonic score."""

    def __init__(
        self,
        size_fn: Optional[Callable[[], float]] = None,
        token_fn: Optional[Callable[[], Optional[float]]] = None,
        cpu_fn: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self._size_fn = size_fn
        self._token_fn = token_fn
        self._cpu_fn = cpu_fn
        self._last_size = 0.0
        self._last_token = 0.0
        self._last_cpu = 0.0
        self._token_invalid = False

    def sample(self) -> float:
        if self._size_fn is not None:
            try:
                size = float(self._size_fn() or 0)
            except Exception:  # noqa: BLE001 - a probe failure means no signal
                size = self._last_size
            if size > self._last_size:
                self._last_size = size

        if self._token_fn is not None:
            try:
                token = self._token_fn()
            except Exception:  # noqa: BLE001
                token = None
            # Domain probes return a validator-produced monotonic score.  Only
            # strict increase counts.  A regression/malformed value latches the
            # channel closed: accepting a later high-water mark would let a
            # sawtooth token (1, 2, 1, 3, 1, 4, ...) renew a stuck job forever.
            # ``None`` and an exception remain temporary unavailability and
            # carry the last good value forward, matching the plugin protocol.
            if token is not None and not self._token_invalid:
                if (isinstance(token, bool)
                        or not isinstance(token, (int, float))):
                    self._token_invalid = True
                else:
                    numeric = float(token)
                    if not math.isfinite(numeric) or numeric < self._last_token:
                        self._token_invalid = True
                    elif numeric > self._last_token:
                        self._last_token = numeric

        if self._cpu_fn is not None:
            try:
                cpu = self._cpu_fn()
            except Exception:  # noqa: BLE001
                cpu = None
            if cpu is not None and cpu > self._last_cpu:
                self._last_cpu = cpu

        return self._last_size + self._last_token + self._last_cpu


def _default_wait(proc, seconds: float) -> Optional[int]:
    try:
        return proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


def supervise(
    proc,
    progress_probe: Callable[[], Optional[float]],
    kill_fn: Callable[[object, str], None],
    *,
    poll_s: float = DEFAULT_POLL_S,
    stall_grace_s: float = DEFAULT_STALL_GRACE_S,
    wait_fn: Optional[Callable[[object, float], Optional[int]]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[str, Optional[int]]:
    """Wait for natural completion, or stop after a flat progress lease.

    There is deliberately no total-runtime ceiling.  ``stall_grace_s`` measures
    only the interval since the last *advance*, not how long the command ran.
    """
    if poll_s <= 0 or stall_grace_s <= 0:
        raise ValueError("poll_s and stall_grace_s must be positive")
    wait = wait_fn or _default_wait
    last_progress_at = clock()
    progress_invalid = False
    last_token: Optional[float] = None
    try:
        initial = progress_probe()
    except Exception:  # noqa: BLE001
        initial = None
    if initial is not None:
        if (isinstance(initial, bool)
                or not isinstance(initial, (int, float))
                or not math.isfinite(float(initial))):
            progress_invalid = True
        else:
            last_token = float(initial)

    while True:
        rc = wait(proc, poll_s)
        if rc is not None:
            return "natural", rc
        now = clock()
        try:
            raw_token = progress_probe()
        except Exception:  # noqa: BLE001
            raw_token = None
        # Enforce the same protocol at this lower boundary as ProgressMeter:
        # only a finite numeric strict increase renews the lease.  A malformed
        # or regressed sequence is untrusted forever; otherwise a direct caller
        # could bypass the meter with a sawtooth token and run indefinitely.
        if raw_token is not None and not progress_invalid:
            if (isinstance(raw_token, bool)
                    or not isinstance(raw_token, (int, float))
                    or not math.isfinite(float(raw_token))):
                progress_invalid = True
            else:
                token = float(raw_token)
                if last_token is not None and token < last_token:
                    progress_invalid = True
                elif last_token is None or token > last_token:
                    last_progress_at = now
                    last_token = token
        if now - last_progress_at > stall_grace_s:
            # Close the wait/probe race: a natural exit that happened just
            # after wait() returned "running" must win over a stall label.
            try:
                natural_rc = proc.poll()
            except (AttributeError, OSError):
                natural_rc = None
            if natural_rc is not None:
                return "natural", natural_rc
            kill_fn(proc, "stalled")
            return "stalled", None


@dataclass(frozen=True)
class _ProcStat:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    start_ticks: int
    own_cpu_ticks: int
    child_cpu_ticks: int


def _read_proc_stat(path: Path) -> Optional[_ProcStat]:
    """Read the fields needed from Linux /proc/<pid>/stat.

    ``comm`` is parenthesised and may contain spaces or ``)``, so split after
    the final closing parenthesis rather than splitting the whole line.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        pid = int(raw.split("(", 1)[0].strip())
        fields = raw[raw.rfind(")") + 2 :].split()
        return _ProcStat(
            pid=pid,
            state=fields[0],
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            session=int(fields[3]),
            own_cpu_ticks=int(fields[11]) + int(fields[12]),
            child_cpu_ticks=int(fields[13]) + int(fields[14]),
            start_ticks=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


_ProcessIdentity = tuple[int, int]  # (pid, /proc starttime ticks)


def _proc_snapshot(proc_root: Path) -> Optional[dict[int, _ProcStat]]:
    """One internally consistent-enough /proc census for a supervision poll."""
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    stats: dict[int, _ProcStat] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        stat = _read_proc_stat(entry / "stat")
        if stat is not None:
            stats[stat.pid] = stat
    return stats


class HostProcessTreeCpuProbe:
    """Monotonic CPU progress and identity ownership for one host job.

    ``start_new_session=True`` makes the root PID both session and process-group
    leader.  We retain every observed ``(pid,starttime)`` identity, so a child
    that later changes process group/session or is reparented remains owned.
    Retention is also what makes termination safe in the face of PID reuse.
    """

    def __init__(self, root_pid: int, proc_root: Path | str = "/proc") -> None:
        self.root_pid = int(root_pid)
        self.proc_root = Path(proc_root)
        initial = _read_proc_stat(self.proc_root / str(root_pid) / "stat")
        self.root_start_ticks = initial.start_ticks if initial else None
        self._root_identity: Optional[_ProcessIdentity] = (
            (self.root_pid, initial.start_ticks) if initial else None
        )
        self._owned_identities: set[_ProcessIdentity] = set()
        if self._root_identity is not None:
            self._owned_identities.add(self._root_identity)
        self._observed_ticks = 0.0
        self._last_by_identity: dict[tuple[int, int], int] = {}
        try:
            self._ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError):
            self._ticks_per_second = 100.0

    @staticmethod
    def _identity(stat: _ProcStat) -> _ProcessIdentity:
        return stat.pid, stat.start_ticks

    def _discover_owned(self, stats: dict[int, _ProcStat]) -> set[int]:
        root = stats.get(self.root_pid)
        if self._root_identity is None and root is not None:
            self.root_start_ticks = root.start_ticks
            self._root_identity = self._identity(root)
            self._owned_identities.add(self._root_identity)
        root_valid = (
            root is not None and self._root_identity == self._identity(root)
        )

        # Start from every retained identity that is still the same process.
        descendants = {
            pid for pid, stat in stats.items()
            if self._identity(stat) in self._owned_identities
        }
        if root_valid:
            descendants.add(self.root_pid)
            # A child may setpgid(), so process-group membership alone is not
            # ownership.  Every member of the private session remains ours.
            descendants.update(
                pid for pid, stat in stats.items()
                if stat.session == self.root_pid
            )

        # Also follow parentage.  This captures a just-forked child that calls
        # setsid(); retaining its identity keeps it owned after reparenting.
        changed = True
        while changed:
            changed = False
            for pid, stat in stats.items():
                if pid not in descendants and stat.ppid in descendants:
                    descendants.add(pid)
                    changed = True
        self._owned_identities.update(
            self._identity(stats[pid]) for pid in descendants
        )
        return descendants

    def live_identities(self) -> set[_ProcessIdentity]:
        """Refresh ownership and return live, non-zombie owned identities."""
        stats = _proc_snapshot(self.proc_root)
        if stats is None:
            return set()
        owned_pids = self._discover_owned(stats)
        return {
            self._identity(stats[pid]) for pid in owned_pids
            if stats[pid].state != "Z"
        }

    def sample(self) -> Optional[float]:
        stats = _proc_snapshot(self.proc_root)
        if stats is None:
            return None
        descendants = self._discover_owned(stats)

        # Accumulate deltas per pid/starttime identity.  A worker leaving /proc
        # cannot make this score fall, and pid reuse cannot donate somebody
        # else's CPU to this job.
        for pid in descendants:
            stat = stats[pid]
            ident = (pid, stat.start_ticks)
            previous = self._last_by_identity.get(ident, 0)
            if stat.own_cpu_ticks > previous:
                self._observed_ticks += stat.own_cpu_ticks - previous
                self._last_by_identity[ident] = stat.own_cpu_ticks

        # Do not merge this with the root's cumulative cutime/cstime high-water.
        # That total can contain a large, previously reaped child we never saw;
        # it would then mask CPU from every newly observed worker until their
        # sum exceeded that stale total.  Per-identity deltas are the only
        # accounting source here: they survive worker churn without falling or
        # waiting for a new worker to overtake an unrelated historic maximum.
        return self._observed_ticks / self._ticks_per_second

    def terminate(self, proc, reason: str, *, term_grace_s: float = 5.0) -> None:
        """Terminate every still-live identity observed as part of this job."""
        _terminate_owned_processes(
            proc, reason, tracker=self, term_grace_s=term_grace_s,
        )


def _stat_for_identity(proc_root: Path,
                       identity: _ProcessIdentity) -> Optional[_ProcStat]:
    stat = _read_proc_stat(proc_root / str(identity[0]) / "stat")
    if stat is None or (stat.pid, stat.start_ticks) != identity:
        return None
    return stat


def _signal_identity(proc_root: Path, identity: _ProcessIdentity,
                     sig: signal.Signals, *, group_already_signalled: bool,
                     root_pgrp: int) -> None:
    """Signal exactly the recorded process, never a PID-reuse successor."""
    stat = _stat_for_identity(proc_root, identity)
    if stat is None or stat.state == "Z" or stat.pid in (0, 1, os.getpid()):
        return
    if group_already_signalled and stat.pgrp == root_pgrp:
        return

    # A second /proc check alone still leaves a check/use race before kill(2).
    # On Linux's real procfs, bind the signal to a pidfd and re-check starttime
    # *after* opening it.  If the PID was reused in between, the pidfd refers to
    # the successor but the identity check fails, so neither process is hit.
    # Synthetic proc roots deliberately use the portable fallback below.
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send = getattr(signal, "pidfd_send_signal", None)
    if (proc_root == Path("/proc") and callable(pidfd_open)
            and callable(pidfd_send)):
        try:
            pidfd = pidfd_open(stat.pid, 0)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            pidfd = None
        if pidfd is not None:
            try:
                confirmed = _stat_for_identity(proc_root, identity)
                if confirmed is None or confirmed.state == "Z":
                    return
                try:
                    pidfd_send(pidfd, sig)
                except ProcessLookupError:
                    return
                except (PermissionError, OSError):
                    pass  # old kernel/seccomp policy: retry the checked fallback
                else:
                    return
            finally:
                try:
                    os.close(pidfd)
                except OSError:
                    pass

    # Portable fallback.  Re-read immediately before kill to narrow PID reuse
    # races on platforms without pidfds (the Linux production path uses them).
    stat = _stat_for_identity(proc_root, identity)
    if stat is None or stat.state == "Z" or stat.pid in (0, 1, os.getpid()):
        return
    try:
        os.kill(stat.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_owned_processes(proc, _reason: str, *,
                               tracker: HostProcessTreeCpuProbe,
                               term_grace_s: float) -> None:
    """TERM then KILL a private group plus owned identities outside that group."""
    tracker.sample()  # capture children present at the termination boundary
    root = _stat_for_identity(
        tracker.proc_root,
        tracker._root_identity or (tracker.root_pid, -1),
    )
    private_group = bool(
        root is not None
        and root.pgrp == tracker.root_pid
        and root.session == tracker.root_pid
        and tracker.root_pid not in (0, 1, os.getpid())
    )

    targets = tracker.live_identities()
    group_signalled = False
    if private_group:
        try:
            os.killpg(tracker.root_pid, signal.SIGTERM)
            group_signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if not group_signalled and root is not None:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError, AttributeError):
            pass
    for identity in targets:
        _signal_identity(
            tracker.proc_root, identity, signal.SIGTERM,
            group_already_signalled=group_signalled,
            root_pgrp=tracker.root_pid,
        )

    # Waiting for TERM cleanup is operational cleanup, never a verdict timeout.
    deadline = time.monotonic() + max(0.0, term_grace_s)
    while time.monotonic() < deadline:
        try:
            proc.poll()  # reap a terminated session leader when possible
        except (AttributeError, OSError):
            pass
        remaining = tracker.live_identities()
        if not remaining:
            return
        targets.update(remaining)
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    # Re-scan once more: children forked during TERM are owned too.  Signal the
    # original private group again even when its leader has already exited; a
    # process group may legally outlive its leader.
    targets.update(tracker.live_identities())
    kill_group_signalled = False
    if private_group:
        try:
            os.killpg(tracker.root_pid, signal.SIGKILL)
            kill_group_signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for identity in targets:
        _signal_identity(
            tracker.proc_root, identity, signal.SIGKILL,
            group_already_signalled=kill_group_signalled,
            root_pgrp=tracker.root_pid,
        )
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError, AttributeError):
        pass


def terminate_process_group(proc, reason: str, *,
                            proc_root: Path | str = "/proc",
                            term_grace_s: float = 5.0) -> None:
    """Terminate the private group and every discoverable session descendant.

    Kept as the public two-argument callback used by existing callers.  The
    run-supervised path uses a persistent tracker as its stronger default, so a
    descendant observed before it reparented or called ``setsid`` remains owned.
    """
    tracker = HostProcessTreeCpuProbe(proc.pid, proc_root=proc_root)
    _terminate_owned_processes(
        proc, reason, tracker=tracker, term_grace_s=term_grace_s,
    )


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def run_supervised(
    cmd,
    *,
    cwd: Optional[Path | str] = None,
    env: Optional[dict[str, str]] = None,
    stall_grace_s: float = DEFAULT_STALL_GRACE_S,
    poll_s: float = DEFAULT_POLL_S,
    output_progress: bool = True,
    domain_progress_probe: Optional[Callable[[], Optional[float]]] = None,
    semantic_only: bool = False,
    process_tree_cpu_progress: bool = False,
    cpu_probe: Optional[Callable[[object], Optional[float]]] = None,
    kill: Optional[Callable[[object, str], None]] = None,
    popen_factory: Optional[Callable[..., object]] = None,
    wait_fn: Optional[Callable[[object, float], Optional[int]]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> SupervisedResult:
    """Launch and progress-supervise a host command.

    Output is captured in OS temporary files, so a verbose child cannot fill a
    pipe and deadlock.  Callers needing live tee output can launch the process
    themselves and reuse :func:`supervise` with :class:`ProgressMeter`.
    """
    if semantic_only:
        if domain_progress_probe is None:
            raise ValueError("semantic_only requires domain_progress_probe")
        if process_tree_cpu_progress or cpu_probe is not None:
            raise ValueError("semantic_only cannot use CPU as progress")
        # Output is still captured, but cannot impersonate a domain checkpoint.
        output_progress = False
    if process_tree_cpu_progress and cpu_probe is not None:
        raise ValueError("choose process-tree CPU or an injected CPU probe, not both")
    if (not output_progress and domain_progress_probe is None
            and not process_tree_cpu_progress and cpu_probe is None):
        raise ValueError("no progress signal configured")

    factory = popen_factory or (lambda argv, **kwargs: subprocess.Popen(argv, **kwargs))
    out_file = tempfile.TemporaryFile()
    err_file = tempfile.TemporaryFile()

    def output_size() -> float:
        try:
            return float(
                os.fstat(out_file.fileno()).st_size
                + os.fstat(err_file.fileno()).st_size
            )
        except OSError:
            return 0.0

    started = time.monotonic()
    try:
        proc = factory(
            cmd,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=env,
            stdout=out_file,
            stderr=err_file,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        out_file.close()
        err_file.close()
        return SupervisedResult(
            127, "", f"COMMAND_NOT_STARTED: {type(exc).__name__}: {exc}",
            "launch_error", time.monotonic() - started,
        )

    tree_probe = HostProcessTreeCpuProbe(proc.pid)
    killer = kill or tree_probe.terminate
    effective_cpu_probe = cpu_probe
    using_tree_cpu = effective_cpu_probe is None and process_tree_cpu_progress
    if effective_cpu_probe is None and process_tree_cpu_progress:
        effective_cpu_probe = lambda _proc: tree_probe.sample()
    meter = ProgressMeter(
        size_fn=output_size if output_progress else None,
        token_fn=domain_progress_probe,
        cpu_fn=(lambda: effective_cpu_probe(proc))
        if effective_cpu_probe is not None else None,
    )

    def progress_sample():
        # Ownership tracking is not a liveness signal.  Keep observing even in
        # semantic-only mode so an escaped/reparented descendant can be killed.
        if not using_tree_cpu:
            tree_probe.sample()
        return meter.sample()

    outcome, rc = supervise(
        proc,
        progress_sample,
        killer,
        poll_s=poll_s,
        stall_grace_s=stall_grace_s,
        wait_fn=wait_fn,
        clock=clock,
    )
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError, AttributeError):
            pass

    def read(file) -> str:
        try:
            file.seek(0)
            return _as_text(file.read())
        except OSError:
            return ""

    out = read(out_file)
    err = read(err_file)
    out_file.close()
    err_file.close()
    elapsed = time.monotonic() - started
    if outcome == "stalled":
        bases = []
        if output_progress:
            bases.append("output")
        if domain_progress_probe is not None:
            bases.append("domain progress")
        if effective_cpu_probe is not None:
            bases.append("process CPU")
        diagnostic = (
            f"\nWATCHDOG_STALLED: no forward progress ({' or '.join(bases)}) "
            f"for > {stall_grace_s:g}s; killed as stalled, "
            "not judged by total runtime."
        )
        return SupervisedResult(RC_STALLED, out, err + diagnostic, outcome, elapsed)
    return SupervisedResult(int(rc if rc is not None else 0), out, err, outcome, elapsed)
