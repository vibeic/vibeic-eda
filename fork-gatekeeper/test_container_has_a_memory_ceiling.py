"""`restart-eda.sh` must never create the EDA container unbounded.

MEASURED 2026-08-19, on the host that runs this fleet: a `yosys` inside the
`vibeic-eda` container grew to 113 GB of a 125 GB machine and the desktop
session was OOM-killed. Three separate facts had to be true at once, and all
three were:

  * `docker inspect vibeic-eda --format '{{.HostConfig.Memory}}'` returned 0 —
    no cgroup limit, so "the container" and "the host" were one 125 GB pool;
  * `ulimit -v` inside the container was `unlimited`, so the tool never got an
    allocation failure it could report and simply kept growing;
  * the MCP server's `docker exec` timeout kills the exec CLIENT, not the
    process in the container. Measured directly: kill the client, then
    `ps` inside the container still lists the process. Past the timeout the
    runaway allocates with nothing watching it.

The container-level ceiling is the one of the three that turns a host-killing
failure into a local one, and it is the one this repository owns. Proven end to
end before this test was written, on the same image the fleet runs:

    --memory 512m   -> killed at 448 MiB, host `available` unchanged
    no --memory     -> the identical allocation reached 4096 MiB and exited 0

So the ceiling is what makes the difference, not the allocator.

These tests drive the real script with a stub `docker` on PATH and read the
`docker run` argv it would issue (`RESTART_EDA_PRINT_RUN=1`, which stops before
anything destructive). Asserting on the emitted argv rather than on a
re-derivation is the point: a ceiling that is computed but never passed to
`docker run` is exactly the bug, and would pass a test that only checked the
arithmetic.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "restart-eda.sh"
_NAME = "vibeic-eda"
_IMAGE = "vibeic/vibeic-eda:9.9.9"

# A stub `docker` that answers exactly the queries restart-eda.sh makes, logs
# every invocation, and refuses to be mistaken for a real daemon.
_STUB = r'''#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
log = os.environ["DOCKER_STUB_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write("\t".join(argv) + "\n")

exists = os.environ.get("STUB_CONTAINER_EXISTS", "1") == "1"
memory = os.environ.get("STUB_CONTAINER_MEMORY", "0")

def out(text):
    sys.stdout.write(text)
    raise SystemExit(0)

if argv[:2] == ["image", "inspect"]:
    if "--format" in argv:
        out("sha256:deadbeef\n")
    raise SystemExit(0)
if argv[:2] == ["container", "inspect"]:
    raise SystemExit(0 if exists else 1)
if argv[0] == "top":
    out("PID   ARGS\n1     sleep infinity\n")
if argv[0] == "inspect":
    fmt = argv[argv.index("--format") + 1]
    table = {
        "{{.Config.Image}}": "vibeic/vibeic-eda:9.9.8\n",
        "{{.Config.User}}": "1000\n",
        "{{.Config.WorkingDir}}": "/foss/designs\n",
        "{{.HostConfig.Memory}}": memory + "\n",
        "{{range .HostConfig.Binds}}{{println .}}{{end}}": "/designs:/foss/designs:rw\n",
        "{{range .Config.Cmd}}{{println .}}{{end}}": "--skip\nsleep\ninfinity\n",
    }
    out(table[fmt])
if argv[0] in ("rm", "run", "ps", "images", "start"):
    raise SystemExit(0)
sys.stderr.write("stub docker: unhandled %r\n" % (argv,))
raise SystemExit(97)
'''


def _mem_total_bytes() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise AssertionError("/proc/meminfo has no MemTotal")


@pytest.fixture
def run_script(tmp_path):
    """Drive restart-eda.sh with a stub docker; return the `docker run` argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "docker"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "docker.log"

    def _run(env_extra=None, args=(), expect_rc=0, stubs=None):
        for name, body in (stubs or {}).items():
            extra = bindir / name
            extra.write_text(body, encoding="utf-8")
            extra.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["DOCKER_STUB_LOG"] = str(log)
        env["RESTART_EDA_PRINT_RUN"] = "1"
        env.pop("EDA_MEMORY", None)
        env.pop("EDA_MEMORY_FRACTION", None)
        env.update(env_extra or {})
        proc = subprocess.run(
            ["bash", str(_SCRIPT), _IMAGE, *args],
            env=env, capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == expect_rc, (
            f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        argv = [ln for ln in proc.stdout.splitlines() if ln]
        return argv, proc, log

    return _run


def _flag(argv, name):
    """The value of `--name <value>` in an argv list, or None."""
    return argv[argv.index(name) + 1] if name in argv else None


def test_the_script_is_the_one_that_creates_the_container():
    """Guard the premise. If the run moves elsewhere these tests go quiet."""
    assert _SCRIPT.is_file()
    body = _SCRIPT.read_text(encoding="utf-8")
    assert "docker run -d --init" in body


def test_a_fresh_container_is_created_under_a_ceiling(run_script):
    argv, _, _ = run_script({"STUB_CONTAINER_EXISTS": "0",
                             "DESIGNS_DIR": "/tmp"})
    assert "--memory" in argv, (
        "a container created from scratch carries no --memory; this is the "
        "unbounded configuration that let one tool take the host down")
    assert _flag(argv, "--memory-swap") == _flag(argv, "--memory"), (
        "--memory-swap must equal --memory, or the container may still drag "
        "the host's swap into thrash before it dies")


def test_the_ceiling_is_the_requested_fraction_of_host_ram(run_script):
    """End-to-end arithmetic, against a fraction the test chooses."""
    argv, _, _ = run_script({"EDA_MEMORY_FRACTION": "25"})
    assert int(_flag(argv, "--memory")) == _mem_total_bytes() * 25 // 100


def test_an_explicit_ceiling_is_passed_through_verbatim(run_script):
    argv, _, _ = run_script({"EDA_MEMORY": "64g"})
    assert _flag(argv, "--memory") == "64g"
    assert _flag(argv, "--memory-swap") == "64g"


def test_opting_out_really_opts_out(run_script):
    """EDA_MEMORY=0 must emit NO flag, not `--memory 0` (which docker rejects)."""
    for optout in ("0", "unlimited", "none"):
        argv, _, _ = run_script({"EDA_MEMORY": optout})
        assert "--memory" not in argv, optout
        assert "--memory-swap" not in argv, optout


def test_an_existing_unlimited_container_is_not_cloned_forward(run_script):
    """The regression that reproduces the incident.

    The live container on the day of the crash reported HostConfig.Memory == 0.
    restart-eda.sh clones an existing container's binds, user, workdir and cmd,
    and the obvious way to add a ceiling -- carry the old one over too -- would
    have carried `unlimited` forward forever, leaving exactly the containers
    that caused the outage still unbounded after the fix.
    """
    argv, _, _ = run_script({"STUB_CONTAINER_EXISTS": "1",
                             "STUB_CONTAINER_MEMORY": "0"})
    assert "--memory" in argv, (
        "recreating a container that was unbounded left it unbounded")
    assert int(_flag(argv, "--memory")) > 0


def test_an_existing_deliberate_ceiling_is_preserved(run_script):
    """The other direction: an operator's own choice survives a recreate."""
    chosen = str(8 * 1024 ** 3)
    argv, _, _ = run_script({"STUB_CONTAINER_EXISTS": "1",
                             "STUB_CONTAINER_MEMORY": chosen})
    assert _flag(argv, "--memory") == chosen
    assert _flag(argv, "--memory-swap") == chosen


def test_the_ceiling_is_a_plain_integer_docker_can_parse(run_script):
    """`docker run --memory 1.34974e+11` is a hard error, not a big number."""
    argv, _, _ = run_script()
    assert re.fullmatch(r"[0-9]+", _flag(argv, "--memory")), (
        f"--memory {_flag(argv, '--memory')!r} is not a plain byte count")


def test_an_awk_that_reformats_the_number_is_refused_not_silently_ignored(run_script):
    """The five-host regression, reproduced by its actual cause.

    MEASURED 2026-08-19: `awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo`
    printed 134973464576 on one host and `1.34974e+11` on five others -- awk
    switches to OFMT once the value outgrows the precision it prints as an
    integer, and the boxes disagree about where that is. The first version of
    this script rejected the scientific-notation string and fell back to "no
    ceiling", so the rollout reported success on all five while leaving every
    one of them in the configuration that had taken a host down.

    Two things had to change and both are asserted here: the arithmetic no
    longer goes through awk's formatter, and a ceiling that CANNOT be computed
    is now a refusal rather than a silent unbounded container.
    """
    sci = "#!/bin/sh\necho 1.34974e+11\n"
    _, proc, log = run_script(stubs={"awk": sci}, expect_rc=1)
    assert "EDA_MEMORY" in proc.stderr, (
        f"refused without telling the operator how to proceed: {proc.stderr!r}")
    argv = [ln for ln in proc.stdout.splitlines() if ln]
    assert "--memory" not in argv
    assert "docker" not in argv, (
        "a container was still going to be created without a ceiling; the "
        "refusal must come BEFORE the run argv is emitted")
    calls = [ln.split("\t") for ln in
             (log.read_text(encoding="utf-8").splitlines() if log.is_file() else [])]
    assert not [c for c in calls if c[:1] == ["run"]]


def test_the_byte_count_is_not_left_to_awk_to_format():
    """Pin the cause, not only the symptom.

    The stubbed-awk test above proves the script REFUSES a reformatted number;
    this one proves the script no longer asks awk to produce one. Bare `print`
    of a product is the unsafe form, because awk switches to OFMT above an
    implementation-dependent magnitude. `printf "%d"` would be fine, and so is
    doing the multiply in the shell, which is what this script does.
    """
    body = _SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"MemTotal[^\n]*?\bprint\s+\$2\s*\*", body), (
        "MemTotal is multiplied inside a bare awk `print`; on a large-memory "
        "host awk emits 1.34974e+11 and the ceiling is lost")


def test_a_nonsense_fraction_is_refused_rather_than_silently_defaulted(run_script):
    """The refusal must name the input. A script that exits 1 for some OTHER
    reason would otherwise satisfy an rc-only assertion, which is how this test
    passed against the pre-fix script that had no fraction handling at all."""
    for bad in ("0", "101", "seventy", "-5"):
        _, proc, _ = run_script({"EDA_MEMORY_FRACTION": bad}, expect_rc=1)
        assert "EDA_MEMORY_FRACTION" in proc.stderr, (
            f"refused {bad!r} without saying why: {proc.stderr!r}")


def test_print_run_mode_removes_nothing(run_script):
    """The dry run must not be able to destroy the container it reports on."""
    _, _, log = run_script()
    calls = [ln.split("\t") for ln in log.read_text(encoding="utf-8").splitlines()]
    assert calls, "the stub docker was never called — the script took another path"
    assert not [c for c in calls if c[:1] == ["rm"]], (
        f"RESTART_EDA_PRINT_RUN removed the container: {calls}")
    assert not [c for c in calls if c[:1] == ["run"]]
