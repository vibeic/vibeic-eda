#!/usr/bin/env python3
"""The check existed and nothing invoked it (vibe-ic#813).

`OpenROAD/etc/check_test_registration_parity.py` can go red and had no caller.
That is the whole defect: a check that is never invoked is indistinguishable
from one that passes -- also vibeic-eda#35, #86 and #87.

So the subject here is NOT the checker. It is the WIRING. A test that runs the
driver and watches it go red proves the driver works, which was never in doubt,
and says nothing about whether the tick calls it. These tests therefore EXECUTE
THE RUNNER'S OWN TEXT: the block is sliced out of `run_tick.sh` verbatim and
run, and so is the exit-code aggregation at the bottom of that file. Delete the
block, comment it out, or drop `selftest_rc` from the aggregation, and these go
red -- which a grep-shaped test would not reliably do.

The fixture forks are SYNTHETIC and their checkers are stubs. That is the honest
scope: the subject is the runner's invocation, not OpenROAD's parity logic,
which has its own three-state proof against the real tree. The stubs do assert
the invocation CONTRACT -- cwd is the clone root, argv is what FORKS.json
declared -- so a wiring that called them from the wrong directory could not pass.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TICK = HERE / "run_tick.sh"
DRIVER = HERE / "check_fork_selftests.py"
DAILY_MERGE = HERE / "daily_merge.py"

STUB = '''#!/usr/bin/env python3
"""Stub that verifies the INVOCATION CONTRACT, then obeys a control file."""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
clone = os.path.dirname(here)
if os.path.realpath(os.getcwd()) != os.path.realpath(clone):
    print("CONTRACT VIOLATION: cwd is %s, expected the clone root %s"
          % (os.getcwd(), clone), file=sys.stderr)
    sys.exit(3)
want = json.load(open(os.path.join(here, "expected_argv.json")))
mine = want[os.path.basename(__file__)]
if sys.argv[1:] != mine:
    print("CONTRACT VIOLATION: argv %r, expected %r" % (sys.argv[1:], mine),
          file=sys.stderr)
    sys.exit(3)
rc = int(open(os.path.join(here, "rc_" + os.path.basename(__file__))).read())
if rc:
    print("noise on stdout that must not be mistaken for the diagnosis")
    print("FAIL: this fork check is red", file=sys.stderr)
    print("  integration  demo:the_unwired_one", file=sys.stderr)
sys.exit(rc)
'''

CHECKERS = {           # basename -> argv[1:] the fixture FORKS.json declares
    "stub_parity.py": ["."],
    "stub_messages.py": ["-d", "src"],
}


# ------------------------------------------------------------------ slicing

def _slice_block(src: str) -> str:
    start = src.index('SELFTEST_OUT="${LOG_DIR}/fork-selftests.txt"')
    rest = src[start:]
    end = re.search(r"^fi$", rest, re.M)
    assert end, "the fork-selftest block in run_tick.sh has no closing `fi`"
    return rest[: end.end()]


def _slice_exit_aggregation(src: str) -> str:
    start = src.index("# A guard failure must not be erased")
    # Anchored at the START OF A LINE. `log "[done] gatekeeper tick exit ${rc}"`
    # also contains `exit ${rc}`, so the naive search cut the slice in the middle
    # of that string and bash reported an unterminated quote -- a fragment that
    # cannot run at all, which would have looked like a wiring failure.
    end = src.index("\nexit ${rc}", start) + len("\nexit ${rc}")
    return src[start:end]


def _run_fragment(fragment: str, tmp_path: Path,
                  env_lines: str = "") -> subprocess.CompletedProcess:
    script = "\n".join([
        "set -uo pipefail",
        'log() { echo "[log] $*"; }',
        f'LOG="{tmp_path}/tick.log"',
        f'LOG_DIR="{tmp_path}/state"',
        f'mkdir -p "{tmp_path}/state"',
        env_lines,
        fragment,
        'echo "SELFTEST_RC=${selftest_rc:-unset}"',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=300)


# ------------------------------------------------------------------ fixtures

def _make_fork(root: Path, *, parity_rc: int = 0, messages_rc: int = 0,
               with_parity_checker: bool = True) -> Path:
    clone = root / "DemoFork"
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    etc = clone / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "expected_argv.json").write_text(json.dumps(CHECKERS), encoding="utf-8")
    names = ["stub_messages.py"] + (["stub_parity.py"] if with_parity_checker else [])
    for name in names:
        (etc / name).write_text(STUB, encoding="utf-8")
        os.chmod(etc / name, 0o755)
    (etc / "rc_stub_parity.py").write_text(str(parity_rc))
    (etc / "rc_stub_messages.py").write_text(str(messages_rc))
    return clone


def _tick_dir(tmp_path: Path, *, with_driver: bool = True,
              malformed: bool = False) -> Path:
    """A stand-in for ${DIR}: the driver, the shared runner it imports, and the
    fixture's own FORKS.json -- so the tests never touch the real registry."""
    d = tmp_path / "dir"
    d.mkdir(parents=True, exist_ok=True)
    if with_driver:
        shutil.copy2(DRIVER, d / "check_fork_selftests.py")
    shutil.copy2(DAILY_MERGE, d / "daily_merge.py")
    checks = [
        {"name": "parity", "path": "etc/stub_parity.py",
         "cmd": ["python3", "etc/stub_parity.py", "."], "why": "the 813 gap"},
        {"name": "dup-ids", "path": "etc/stub_messages.py",
         "cmd": ["python3", "etc/stub_messages.py", "-d", "src"], "why": "rcx ids"},
    ]
    if malformed:
        checks[0] = {"name": "parity", "cmd": "not-a-list"}
    (d / "FORKS.json").write_text(json.dumps(
        {"forks": [{"tool": "DemoFork", "post_merge_check": checks}]}), encoding="utf-8")
    return d


def _block_env(tick_dir: Path, forks: Path) -> str:
    return "\n".join([f'DIR="{tick_dir}"', f'export GK_FORKS_DIR="{forks}"'])


# ------------------------------------------------------ the wiring, executed

def test_the_runner_actually_contains_an_invocation():
    """Necessary, nowhere near sufficient -- the rest of this file is why."""
    assert "check_fork_selftests.py" in _slice_block(TICK.read_text())


def test_the_runner_block_passes_when_every_declared_check_is_clean(tmp_path):
    forks = tmp_path / "forks"
    _make_fork(forks)
    cp = _run_fragment(_slice_block(TICK.read_text()), tmp_path,
                       _block_env(_tick_dir(tmp_path), forks))
    assert "SELFTEST_RC=0" in cp.stdout, cp.stdout + cp.stderr
    assert "CONTRACT VIOLATION" not in (cp.stdout + cp.stderr)


def test_the_runner_block_goes_red_when_a_declared_check_is_red(tmp_path):
    """THE POINT. Break the fork; run the RUNNER, not the driver; the round must
    go red and must name what broke."""
    forks = tmp_path / "forks"
    _make_fork(forks, parity_rc=1)
    cp = _run_fragment(_slice_block(TICK.read_text()), tmp_path,
                       _block_env(_tick_dir(tmp_path), forks))
    assert "SELFTEST_RC=1" in cp.stdout, cp.stdout + cp.stderr
    assert "FAIL" in cp.stdout and "DemoFork:parity" in cp.stdout


@pytest.mark.parametrize("kw,want_in_log", [
    (dict(), "clone absent"),
    (dict(with_parity_checker=False), "MISSING: etc/stub_parity.py"),
])
def test_could_not_check_is_its_own_state_and_is_not_a_pass(tmp_path, kw,
                                                            want_in_log):
    """The checker lives inside the tree it audits, so a merge that deleted it
    produces no output at all -- which is how this class of defect has hidden
    every previous time."""
    forks = tmp_path / "forks"
    forks.mkdir()
    if kw:
        _make_fork(forks, **kw)
    cp = _run_fragment(_slice_block(TICK.read_text()), tmp_path,
                       _block_env(_tick_dir(tmp_path), forks))
    assert "SELFTEST_RC=2" in cp.stdout, cp.stdout + cp.stderr
    assert "COULD-NOT-CHECK" in cp.stdout
    assert want_in_log in cp.stdout, cp.stdout


def test_a_malformed_declaration_is_not_a_pass(tmp_path):
    """A typo in FORKS.json is silently identical to deleting the gate."""
    forks = tmp_path / "forks"
    _make_fork(forks)
    cp = _run_fragment(_slice_block(TICK.read_text()), tmp_path,
                       _block_env(_tick_dir(tmp_path, malformed=True), forks))
    assert "SELFTEST_RC=2" in cp.stdout, cp.stdout + cp.stderr
    assert "MALFORMED" in cp.stdout


def test_a_missing_driver_is_not_a_pass_either(tmp_path):
    forks = tmp_path / "forks"
    _make_fork(forks)
    cp = _run_fragment(_slice_block(TICK.read_text()), tmp_path,
                       _block_env(_tick_dir(tmp_path, with_driver=False), forks))
    assert "SELFTEST_RC=2" in cp.stdout, cp.stdout + cp.stderr
    assert "nothing was checked" in cp.stdout


@pytest.mark.parametrize("rc_in,want", [(0, 0), (1, 8), (2, 8)])
def test_the_verdict_reaches_the_tick_exit_code(rc_in, want):
    """Running it is not enough -- the result has to be load-bearing. This
    executes the runner's OWN aggregation, so an edit that drops `selftest_rc`
    fails here rather than shipping a tick that reports 0 on a morning a fork's
    suite was unwired."""
    script = "\n".join([
        "set -uo pipefail", 'log() { :; }',
        "rc=0", "guard_rc=0", "merge_rc=0", "release_rc=0", "ship_rc=0",
        "cap_rc=0", f"selftest_rc={rc_in}",
        _slice_exit_aggregation(TICK.read_text()),
    ])
    cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert cp.returncode == want, (
        f"selftest_rc={rc_in} produced tick exit {cp.returncode}, expected "
        f"{want}\n{cp.stdout}{cp.stderr}")


def test_a_higher_priority_failure_is_not_erased_by_ours():
    """The aggregation assigns only while rc is still 0."""
    script = "\n".join([
        "set -uo pipefail", 'log() { :; }',
        "rc=9", "guard_rc=0", "merge_rc=0", "release_rc=0", "ship_rc=0",
        "cap_rc=0", "selftest_rc=1",
        _slice_exit_aggregation(TICK.read_text()),
    ])
    assert subprocess.run(["bash", "-c", script]).returncode == 9


def test_our_code_does_not_collide_with_the_capability_gate():
    """`cap_rc` took rc=7 while this was in flight. Two checks sharing an exit
    code make the round's own report ambiguous."""
    src = TICK.read_text()
    codes = re.findall(r'\[ "\$\{(\w+):-0\}" != "0" \] && \[ "\$\{rc\}" = "0" \] && rc=(\d+)',
                       src)
    seen = {}
    for var, code in codes:
        assert code not in seen, (
            f"rc={code} is claimed by both {seen[code]} and {var}")
        seen[code] = var


def test_the_tick_still_parses():
    cp = subprocess.run(["bash", "-n", str(TICK)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr


# ------------------------------------------- one declaration, not two (#89)

def _driver():
    spec = importlib.util.spec_from_file_location("_cfs", str(DRIVER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_driver_declares_nothing_of_its_own():
    """vibeic-eda#89 owns the declaration. A second list here would be a second
    thing to forget, and the two would drift the first time one was edited --
    the fault `build_branches` already carries a warning about, from #30."""
    src = DRIVER.read_text()
    assert "check_test_registration_parity" not in src, (
        "the driver names a specific fork check; declarations belong in "
        "FORKS.json, which daily_merge.py already reads")
    assert "find_messages" not in src, "same -- no second registry"
    assert "run_post_merge_checks" in src, (
        "the driver must REUSE daily_merge's runner, not reimplement the "
        "judgement; two implementations of 'is this check clean' will disagree")


def test_the_real_registry_declares_the_parity_check():
    mod = _driver()
    decls = dict(mod.declared())
    assert "OpenROAD" in decls, "OpenROAD declares no post_merge_check"
    names = {c["name"] for c in decls["OpenROAD"]}
    assert "test-registration-parity" in names, (
        f"vibe-ic#813's oracle is not declared; FORKS.json has {names}")


def test_the_merge_gate_cannot_reach_a_day_with_no_upstream_commits():
    """The reason this program exists ALONGSIDE #89 rather than instead of it.

    `merge_one` returns ALREADY_CURRENT before it ever calls
    `run_post_merge_checks`, so the declared checks are MERGE-TRIGGERED. All 27
    unwired tests in vibe-ic#813 arrived through our own patches, not an
    upstream merge, so that gate would have caught none of them.

    This is a SOURCE-ORDER claim, not an execution -- honest about what it is.
    If it ever fails because `merge_one` learned to check unconditionally, the
    right response is to re-examine whether the daily call site is still needed,
    not to delete this test.
    """
    src = DAILY_MERGE.read_text()
    body = src[src.index("def merge_one("):]
    already = body.index("ALREADY_CURRENT")
    runs = body.index("run_post_merge_checks(")
    assert already < runs, (
        "merge_one now reaches run_post_merge_checks even when level with "
        "upstream — re-examine whether the daily tick call site is still needed")


# ------------------------------------- the WHOLE tick, executed end to end
#
# The slice tests above run the runner's own text; these run the runner. The
# harness is `test_capability_check_blocks_the_tick.py`'s, which already proved
# out for vibeic-eda#88: stub every program whose status the tick folds into its
# exit code, leave one real, and assert on `run_tick.sh`'s OWN exit. A green
# control comes first — without it, an assertion of "non-zero" is satisfied by a
# tick that fell over for an unrelated reason.

_OK_STUB = "#!/usr/bin/env python3\nimport sys\nprint('stub ok')\nsys.exit(0)\n"


def _stub(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _tick_fleet(tmp_path: Path, selftest_body: str):
    home, root = tmp_path / "home", tmp_path / "eda"
    dirp = root / "fork-gatekeeper"
    dirp.mkdir(parents=True)
    shutil.copy2(TICK, dirp / "run_tick.sh")
    (root / "RELEASED.json").write_text('{"version": "0.0.1-test"}\n')
    for name in ("check_fork_only.py", "check_pins_agree.py",
                 "check_doc_counts.py", "check_fork_presence_claims.py"):
        _stub(root / "tools" / name, _OK_STUB)
    for name in ("daily_merge.py", "daily_release.py",
                 "check_our_commits_ship.py", "check_no_capability_lost.py",
                 "gatekeeper.py"):
        _stub(dirp / name, _OK_STUB)
    _stub(dirp / "check_fork_selftests.py", selftest_body)
    _stub(home / ".local" / "bin" / "gh",
          '#!/usr/bin/env bash\n[ "$1" = auth ] && echo gho_stubtoken\nexit 0\n')
    env = dict(os.environ)
    env.update(HOME=str(home), GK_STATE_DIR=str(tmp_path / "state"))
    return dirp / "run_tick.sh", env


def _run_tick(tmp_path: Path, selftest_body: str):
    script, env = _tick_fleet(tmp_path, selftest_body)
    return subprocess.run(["bash", str(script)], env=env, capture_output=True,
                          text=True, timeout=900)


def _exiting(rc: int, msg: str = "") -> str:
    return ("#!/usr/bin/env python3\nimport sys\n"
            f"print({msg!r})\nsys.exit({rc})\n")


def test_the_whole_tick_exits_zero_when_the_forks_checks_are_clean(tmp_path):
    """The control. Without it every assertion below is satisfied by a tick that
    fell over somewhere else."""
    p = _run_tick(tmp_path, _OK_STUB)
    assert p.returncode == 0, f"stubbed-green tick exited {p.returncode}\n{p.stdout[-2500:]}"


@pytest.mark.parametrize("rc,label", [(1, "a fork check is RED"),
                                      (2, "a fork check COULD NOT RUN")])
def test_the_whole_tick_goes_red_when_a_forks_check_does(tmp_path, rc, label):
    """THE ASK. Not the driver going red — the ROUND going red, from the real
    `run_tick.sh`, with everything else stubbed green so an 8 can only have come
    from this block."""
    p = _run_tick(tmp_path, _exiting(rc, "  FAIL  OpenROAD:test-registration-parity"))
    assert p.returncode == 8, (
        f"{label}: tick exited {p.returncode}, expected 8\n{p.stdout[-2500:]}")
    assert "[selftest]" in p.stdout


def test_the_whole_tick_goes_red_when_the_driver_is_absent(tmp_path):
    """A deleted driver must not read as a clean day."""
    script, env = _tick_fleet(tmp_path, _OK_STUB)
    (script.parent / "check_fork_selftests.py").unlink()
    p = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                       text=True, timeout=900)
    assert p.returncode == 8, f"{p.returncode}\n{p.stdout[-2500:]}"
    assert "nothing was checked" in p.stdout


def _dm():
    spec = importlib.util.spec_from_file_location("_dm", str(DAILY_MERGE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_failure_detail_names_the_defect_not_the_advice():
    """`tail[-4:]` reported the wrong lines, and a wrong summary is worse than
    none because it reads as a diagnosis.

    Measured on the real checker with one test un-wired: the last four lines are
    "Wire each into its module's BUILD ... an undeclared data file fails the
    test", which is true, generic, and names neither module nor test.
    `tap:bound_to_placement` was four lines above.
    """
    out = "\n".join([
        "CMake-registered tests checked : 1400",
        "  UNEXPECTED                   : 1",
        "FAIL: registered in CMake, absent from the bazel build:",
        "  integration  tap:bound_to_placement",
        "Wire each into its module's BUILD, or add it to KNOWN_CMAKE_ONLY",
        "with a reason about the TEST. Declare every file the test reads:",
        "bazel sandboxes, and an undeclared data file fails the test for a",
        "reason that has nothing to do with the code under test.",
    ])
    detail = _dm()._salient(out, "")
    assert "tap:bound_to_placement" in detail, detail
    assert "bazel sandboxes" not in detail, (
        "the summary is still showing the trailing advice: " + detail)


def test_the_detail_searches_stderr_separately_from_stdout():
    """find_messages.py prints a 3000-line inventory to stdout and the collision
    to stderr. A merged search is a haystack that does not contain the needle."""
    noise = "\n".join(f"ANT {i:04d} something ERROR http://x" for i in range(200))
    err = ("Error: RCX 0515 used 2 times, next free message id is 516\n"
           "  Appears in multiChipExtractor.cpp on line 15")
    detail = _dm()._salient(noise, err)
    assert "RCX 0515 used 2 times" in detail, detail
    assert "ANT 0000" not in detail, detail


def test_an_unrecognised_failure_says_it_is_a_guess():
    detail = _dm()._salient("something went sideways\nand then stopped", "")
    assert detail.startswith("(no recognised marker"), detail


def test_no_declared_path_is_one_no_revision_ever_carried():
    """A TYPO in FORKS.json is permanent COULD-NOT-CHECK, and this is the only
    place that can tell it apart from a checker that has not landed on the
    checked-out branch yet -- two identical-looking states with opposite fixes.

    Deliberately NOT asserting "the file is in the working tree": that is a
    FLEET fact with a home (the tick reports COULD-NOT-CHECK and carries rc=8),
    and asserting it here would make this suite red -- and, through
    `run_0530.sh`, block the daily page publish -- for a fork branch mid-landing.
    """
    mod = _driver()
    forks = Path(os.environ.get("GK_FORKS_DIR") or mod.DEFAULT_FORKS_DIR)
    unknown = []
    for tool, checks in mod.declared():
        clone = forks / tool
        if not clone.is_dir():
            pytest.skip(f"{clone} is not checked out on this machine")
        for c in checks:
            rel = c.get("path")
            if not rel or (clone / rel).exists():
                continue
            seen = subprocess.run(
                ["git", "log", "--all", "--oneline", "-1", "--", rel],
                cwd=str(clone), capture_output=True, text=True, timeout=120)
            if not seen.stdout.strip():
                unknown.append(f"{tool}:{rel}")
    assert not unknown, (
        "these declarations name a path NO revision of the fork has ever "
        f"carried — a typo here is COULD-NOT-CHECK forever: {unknown}")
