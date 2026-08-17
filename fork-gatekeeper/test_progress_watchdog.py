"""Proofs for the shared forward-progress supervisor."""
from __future__ import annotations

import math
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress_watchdog as W  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Proc:
    def __init__(self) -> None:
        self.killed = False

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fake", timeout)


def _write_proc_stat(
    proc_root: Path,
    pid: int,
    *,
    ppid: int,
    pgrp: int,
    session: int,
    start: int,
    own: int = 0,
    children: int = 0,
    state: str = "S",
) -> None:
    """Write the /proc fields consumed by HostProcessTreeCpuProbe."""
    path = proc_root / str(pid) / "stat"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        state, str(ppid), str(pgrp), str(session),
        "0", "0", "0", "0", "0", "0", "0",
        str(own), "0", str(children), "0",
        "0", "0", "1", "0", str(start),
    ]
    # A ')' in comm proves the parser splits after the final close paren.
    path.write_text(f"{pid} (worker ) name) {' '.join(fields)}\n")


def _remove_proc(proc_root: Path, pid: int) -> None:
    (proc_root / str(pid) / "stat").unlink()
    (proc_root / str(pid)).rmdir()


def test_progressing_work_can_run_past_the_old_runtime_limit() -> None:
    """Elapsed runtime alone can never kill a command."""
    clock = FakeClock()
    proc = Proc()
    score = {"value": 0}
    polls = {"count": 0}
    kills: list[str] = []

    def wait(_proc, seconds):
        clock.now += seconds
        polls["count"] += 1
        score["value"] += 1
        return 0 if polls["count"] == 2200 else None

    outcome, rc = W.supervise(
        proc,
        lambda: score["value"],
        lambda _proc, reason: kills.append(reason),
        poll_s=10,
        stall_grace_s=30,
        wait_fn=wait,
        clock=clock,
    )
    assert clock.now == 22000  # beyond the former 21,600 s harness deadline
    assert (outcome, rc, kills) == ("natural", 0, [])


def test_flat_progress_is_a_distinct_stall_not_a_test_failure() -> None:
    clock = FakeClock()
    kills: list[str] = []

    def wait(_proc, seconds):
        clock.now += seconds
        return None

    outcome, rc = W.supervise(
        Proc(),
        lambda: 7,
        lambda _proc, reason: kills.append(reason),
        poll_s=10,
        stall_grace_s=30,
        wait_fn=wait,
        clock=clock,
    )
    assert (outcome, rc) == ("stalled", None)
    assert kills == ["stalled"]


def test_natural_exit_wins_the_wait_probe_stall_race() -> None:
    """A process exiting after wait() sampled it must not be killed as stalled."""
    clock = FakeClock()
    kills: list[str] = []

    class RacingProc(Proc):
        def poll(self):
            return 7

    def wait(_proc, seconds):
        clock.now += seconds
        return None

    outcome, rc = W.supervise(
        RacingProc(), lambda: 0,
        lambda _proc, reason: kills.append(reason),
        poll_s=40, stall_grace_s=30, wait_fn=wait, clock=clock,
    )
    assert (outcome, rc, kills) == ("natural", 7, [])


def test_supervisor_itself_rejects_a_regressing_progress_protocol() -> None:
    """Direct callers cannot bypass the meter with a sawtooth token."""
    clock = FakeClock()
    readings = iter([1, 2, 1, 3, 4, 5])
    kills: list[str] = []

    def wait(_proc, seconds):
        clock.now += seconds
        return None

    outcome, rc = W.supervise(
        Proc(), lambda: next(readings),
        lambda _proc, reason: kills.append(reason),
        poll_s=10, stall_grace_s=30, wait_fn=wait, clock=clock,
    )
    assert (outcome, rc, kills) == ("stalled", None, ["stalled"])


def test_none_flapping_cannot_fake_progress() -> None:
    readings = iter([None, 4.0, None, 4.0, None])
    meter = W.ProgressMeter(cpu_fn=lambda: next(readings))
    samples = [meter.sample() for _ in range(5)]
    assert samples == [0.0, 4.0, 4.0, 4.0, 4.0]


def test_real_output_progress_finishes_after_many_grace_windows() -> None:
    child = (
        "import sys,time\n"
        "for i in range(12):\n"
        " print(i, flush=True); time.sleep(.08)\n"
    )
    result = W.run_supervised(
        [sys.executable, "-c", child], poll_s=0.04, stall_grace_s=0.2
    )
    assert result.outcome == "natural"
    assert result.rc == 0
    assert "11" in result.out
    assert result.elapsed_s > 0.7


def test_real_silent_idle_process_is_stalled() -> None:
    result = W.run_supervised(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        poll_s=0.04,
        stall_grace_s=0.15,
    )
    assert result.outcome == "stalled"
    assert result.rc == W.RC_STALLED
    assert "WATCHDOG_STALLED" in result.err
    assert "total runtime" in result.err


def test_silent_cpu_work_is_forward_progress() -> None:
    result = W.run_supervised(
        [sys.executable, "-c", "x=0\nfor i in range(12000000): x += i\n"],
        poll_s=0.02,
        stall_grace_s=0.08,
        process_tree_cpu_progress=True,
    )
    assert result.outcome == "natural", result.err
    assert result.rc == 0


def test_natural_nonzero_exit_is_preserved() -> None:
    result = W.run_supervised(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        poll_s=0.02,
        stall_grace_s=1,
    )
    assert result.outcome == "natural"
    assert result.rc == 7


def test_cpu_is_opt_in_for_semantic_only_supervision() -> None:
    """A busy loop cannot impersonate a validated domain checkpoint."""
    result = W.run_supervised(
        [sys.executable, "-c", "while True: pass"],
        output_progress=False,
        domain_progress_probe=lambda: 0,
        process_tree_cpu_progress=False,
        poll_s=0.03,
        stall_grace_s=0.12,
    )
    assert result.outcome == "stalled"


def test_flapping_domain_token_cannot_renew_the_lease() -> None:
    readings = iter([1, 2, 1, 2, 1, 2])
    meter = W.ProgressMeter(token_fn=lambda: next(readings))
    samples = [meter.sample() for _ in range(6)]
    assert samples == [1, 2, 2, 2, 2, 2]


def test_regressed_domain_channel_stays_closed_even_above_its_old_high_water() -> None:
    readings = iter([1, 2, 1, 3, 4])
    meter = W.ProgressMeter(token_fn=lambda: next(readings))
    assert [meter.sample() for _ in range(5)] == [1, 2, 2, 2, 2]


@pytest.mark.parametrize("bad", [True, "2", math.nan, math.inf])
def test_malformed_domain_token_latches_the_channel_closed(bad) -> None:
    readings = iter([1, bad, 3])
    meter = W.ProgressMeter(token_fn=lambda: next(readings))
    assert [meter.sample() for _ in range(3)] == [1, 1, 1]


def test_semantic_only_ignores_both_chatty_output_and_busy_cpu() -> None:
    """Diagnostics remain captured but cannot impersonate domain progress."""
    child = (
        "import sys\n"
        "while True:\n"
        " sys.stdout.write('noise\\n'); sys.stdout.flush()\n"
    )
    result = W.run_supervised(
        [sys.executable, "-c", child],
        semantic_only=True,
        domain_progress_probe=lambda: 0,
        poll_s=0.03,
        stall_grace_s=0.12,
    )
    assert result.outcome == "stalled"
    assert result.rc == W.RC_STALLED
    assert "noise" in result.out
    assert "domain progress" in result.err
    assert "output or" not in result.err


def test_semantic_only_requires_a_domain_channel() -> None:
    with pytest.raises(ValueError, match="domain_progress_probe"):
        W.run_supervised([sys.executable, "-c", "pass"], semantic_only=True)


def test_semantic_only_refuses_cpu_lease_sources() -> None:
    with pytest.raises(ValueError, match="cannot use CPU"):
        W.run_supervised(
            [sys.executable, "-c", "pass"], semantic_only=True,
            domain_progress_probe=lambda: 0,
            process_tree_cpu_progress=True,
        )


def test_process_cpu_score_advances_across_worker_churn_and_stale_cutime(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(W.os, "sysconf", lambda _name: 100)
    # A large historic cutime from an unobserved, already-reaped worker must
    # not become a high-water barrier for the workers that we can identify.
    _write_proc_stat(
        tmp_path, 100, ppid=1, pgrp=100, session=100, start=1000,
        own=1, children=10000)
    _write_proc_stat(
        tmp_path, 101, ppid=100, pgrp=100, session=100,
        start=2000, own=100)
    probe = W.HostProcessTreeCpuProbe(100, proc_root=tmp_path)
    first = probe.sample()

    # The high-CPU worker exits and a fresh worker begins near zero. A sum of
    # currently-live ticks would fall from 101 to 11. A max with the root's
    # 10,000 cutime ticks would stay flat much longer. The per-identity delta
    # score advances immediately through both forms of stale high-water.
    _remove_proc(tmp_path, 101)
    _write_proc_stat(
        tmp_path, 102, ppid=100, pgrp=100, session=100,
        start=3000, own=10)
    second = probe.sample()
    _write_proc_stat(
        tmp_path, 102, ppid=100, pgrp=100, session=100,
        start=3000, own=20)
    third = probe.sample()
    assert first == pytest.approx(1.01)
    assert second == pytest.approx(1.11)
    assert third == pytest.approx(1.21)

    # Reusing 102 outside this private session is not this job's CPU.
    _remove_proc(tmp_path, 102)
    _write_proc_stat(
        tmp_path, 102, ppid=1, pgrp=999, session=999,
        start=9000, own=500)
    assert probe.sample() == third


def test_process_identity_remains_owned_after_setsid_and_reparent(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(W.os, "sysconf", lambda _name: 100)
    _write_proc_stat(
        tmp_path, 110, ppid=1, pgrp=110, session=110, start=1000, own=1)
    # It is initially discoverable by parentage even though it already made a
    # private process group/session.
    _write_proc_stat(
        tmp_path, 111, ppid=110, pgrp=111, session=111,
        start=2000, own=3)
    probe = W.HostProcessTreeCpuProbe(110, proc_root=tmp_path)
    first = probe.sample()

    # After reparenting, neither ppid, pgrp nor sid links it to the root. The
    # retained (pid,starttime) identity is the only honest ownership evidence.
    _write_proc_stat(
        tmp_path, 111, ppid=1, pgrp=111, session=111,
        start=2000, own=8)
    second = probe.sample()
    assert first == pytest.approx(0.04)
    assert second == pytest.approx(0.09)


class _KillProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = 0
        self.waited = 0

    def terminate(self) -> None:
        self.terminated += 1

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited += 1
        raise subprocess.TimeoutExpired("fake", timeout)


def test_termination_signals_new_process_groups_and_observed_escaped_children(
        tmp_path, monkeypatch) -> None:
    _write_proc_stat(
        tmp_path, 200, ppid=1, pgrp=200, session=200, start=1000)
    _write_proc_stat(
        tmp_path, 201, ppid=200, pgrp=200, session=200, start=2000)
    # Same private session, different pgrp: killpg(200) alone misses it.
    _write_proc_stat(
        tmp_path, 202, ppid=200, pgrp=202, session=200, start=3000)
    # A setsid child is first observed by parentage.
    _write_proc_stat(
        tmp_path, 203, ppid=200, pgrp=203, session=203, start=4000)
    _write_proc_stat(
        tmp_path, 999, ppid=1, pgrp=999, session=999, start=9000)
    tracker = W.HostProcessTreeCpuProbe(200, proc_root=tmp_path)
    tracker.sample()
    # It then reparents and has no remaining structural link to the root.
    _write_proc_stat(
        tmp_path, 203, ppid=1, pgrp=203, session=203, start=4000)

    groups = []
    pids = []
    monkeypatch.setattr(W.os, "killpg", lambda pgid, sig: groups.append((pgid, sig)))
    monkeypatch.setattr(W.os, "kill", lambda pid, sig: pids.append((pid, sig)))
    proc = _KillProc(200)
    tracker.terminate(proc, "stalled", term_grace_s=0)

    assert groups == [
        (200, signal.SIGTERM), (200, signal.SIGKILL),
    ]
    assert (202, signal.SIGTERM) in pids
    assert (202, signal.SIGKILL) in pids
    assert (203, signal.SIGTERM) in pids
    assert (203, signal.SIGKILL) in pids
    assert all(pid != 999 for pid, _sig in pids)
    assert proc.terminated == 0, "the verified private group was signalled"


def test_termination_does_not_signal_a_reused_former_child_pid(
        tmp_path, monkeypatch) -> None:
    _write_proc_stat(
        tmp_path, 210, ppid=1, pgrp=210, session=210, start=1000)
    _write_proc_stat(
        tmp_path, 211, ppid=210, pgrp=211, session=211, start=2000)
    tracker = W.HostProcessTreeCpuProbe(210, proc_root=tmp_path)
    tracker.sample()
    # The owned identity exited. The same numeric PID now belongs elsewhere.
    _write_proc_stat(
        tmp_path, 211, ppid=1, pgrp=999, session=999, start=8000)

    pids = []
    monkeypatch.setattr(W.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(W.os, "kill", lambda pid, sig: pids.append((pid, sig)))
    tracker.terminate(_KillProc(210), "stalled", term_grace_s=0)
    assert all(pid != 211 for pid, _sig in pids)


def test_pidfd_recheck_closes_identity_change_before_signal(monkeypatch) -> None:
    owned = W._ProcStat(                         # noqa: SLF001
        220, "S", 1, 220, 220, 1000, 0, 0,
    )
    # _stat_for_identity returns None when the same numeric PID now has a new
    # starttime; the second value models reuse after pidfd_open().
    stats = iter([owned, None])
    sent = []
    closed = []
    fallback = []
    monkeypatch.setattr(W, "_stat_for_identity",  # noqa: SLF001
                        lambda _root, _ident: next(stats))
    monkeypatch.setattr(W.os, "pidfd_open", lambda _pid, _flags: 77)
    monkeypatch.setattr(
        W.signal, "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
    )
    monkeypatch.setattr(W.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(W.os, "kill",
                        lambda pid, sig: fallback.append((pid, sig)))

    W._signal_identity(                           # noqa: SLF001
        Path("/proc"), (220, 1000), signal.SIGKILL,
        group_already_signalled=False, root_pgrp=220,
    )
    assert sent == []
    assert fallback == []
    assert closed == [77]
