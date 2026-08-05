#!/usr/bin/env python3
"""The capability sweep must be able to STOP the 05:30 round (vibeic-eda#88).

Until 2026-08-05 `run_tick.sh` invoked `check_no_capability_lost.py` with a
trailing `|| true`. It printed `LOST:` lines and the round carried on — a check
that looks like a gate in the log and is not one, which is worse than no gate:
the tick's exit code read clean while a command the base image provided had
stopped resolving in ours.

The correct shape already existed one layer over, in `capability_gate.py`:
rc!=0 emits `built_red` and nothing is promoted, and **rc=2 (the image could not
be probed) blocks exactly like rc=1**, because measuring nothing proves nothing.

THIS TEST EXECUTES THE REAL SCRIPT. It does not read the source and assert that
a `|| true` is absent — that would pass against a `cap_rc` nobody folds into the
exit code, which is the same "correct check that nothing invokes" defect one
level up. The tick is run four times, end to end, with the capability program
stubbed to each interesting exit status, and the assertion is on `run_tick.sh`'s
OWN exit code.

Everything else the tick's exit code depends on is stubbed GREEN, so a 7 can
only have come from the capability block:
  guard_rc -> 3, merge_rc -> 4, release_rc -> 5, ship_rc -> 6, cap_rc -> 7,
  selftest_rc -> 8 (vibe-ic#813).
The `test_a_green_tick_exits_zero` case is what proves that stubbing worked;
without it every other assertion here would also pass against a tick that
exits non-zero for some unrelated reason.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TICK = HERE / "run_tick.sh"

OK = "#!/usr/bin/env python3\nimport sys\nprint('stub ok')\nsys.exit(0)\n"


def _stub(path: Path, body: str, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def _fleet(tmp_path: Path, cap: str | None) -> tuple[Path, dict]:
    """A tree the real run_tick.sh can run in, with `cap` as the capability program."""
    home = tmp_path / "home"
    root = tmp_path / "eda"
    dirp = root / "fork-gatekeeper"
    dirp.mkdir(parents=True)

    shutil.copy2(TICK, dirp / "run_tick.sh")
    (root / "RELEASED.json").write_text('{"version": "0.0.1-test"}\n')

    # Everything whose status the tick folds into its own exit code.
    for name in ("check_fork_only.py", "check_pins_agree.py", "check_doc_counts.py",
                 "check_fork_presence_claims.py"):
        _stub(root / "tools" / name, OK)
    for name in ("daily_merge.py", "daily_release.py", "check_our_commits_ship.py",
                 "gatekeeper.py", "check_fork_selftests.py"):
        _stub(dirp / name, OK)
    if cap is not None:
        _stub(dirp / "check_no_capability_lost.py", cap)

    # run_tick.sh pins PATH to "${HOME}/.local/bin:...:/usr/bin:/bin", so a stub
    # `gh` goes there. Without a token the script exits 2 before anything runs.
    _stub(home / ".local" / "bin" / "gh",
          "#!/usr/bin/env bash\n[ \"$1\" = auth ] && echo gho_stubtoken\nexit 0\n")

    env = dict(os.environ)
    env.update(HOME=str(home), GK_STATE_DIR=str(tmp_path / "state"))
    return dirp / "run_tick.sh", env


def _run(tmp_path, cap):
    script, env = _fleet(tmp_path, cap)
    p = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                       text=True, timeout=900)
    return p


def test_a_green_tick_exits_zero(tmp_path):
    """The control. Without this, every assertion below is satisfied by a tick
    that fails for an unrelated reason, and the stubbing is unverified."""
    p = _run(tmp_path, OK)
    assert p.returncode == 0, f"stubbed-green tick exited {p.returncode}\n{p.stdout[-3000:]}"


def test_a_lost_capability_stops_the_round(tmp_path):
    """rc=1 — something the base provided no longer resolves in our image."""
    p = _run(tmp_path, "#!/usr/bin/env python3\nimport sys\n"
                       "print('LOST: sby')\nsys.exit(1)\n")
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — the round was not stopped\n{p.stdout[-3000:]}"
    assert "LOST: sby" in p.stdout


def test_an_unprobeable_image_stops_the_round_exactly_like_a_loss(tmp_path):
    """rc=2 is 'nothing was compared'. It must NOT be the quiet one — that is
    precisely how a gate stays green for months without ever having run."""
    p = _run(tmp_path, "#!/usr/bin/env python3\nimport sys\n"
                       "print('could not probe the image')\nsys.exit(2)\n")
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — rc=2 was treated as a pass\n{p.stdout[-3000:]}"


def test_a_missing_capability_program_stops_the_round(tmp_path):
    """A checker that is not there checked nothing. The tick already says so in
    its log; before #88 it said so and exited 0 anyway."""
    p = _run(tmp_path, None)
    assert p.returncode == 7, \
        f"exit {p.returncode}, not 7 — a missing checker passed\n{p.stdout[-3000:]}"
    assert "nothing was checked" in p.stdout


def test_the_call_site_captures_the_status_on_the_same_line(tmp_path):
    """`python3 ... | head` then `rc=$?` reads the status of the WRONG process.

    build_and_regress.sh's first draft shipped that bug and this file's own
    `[ship]` block spells the rule out, so the shape is pinned rather than
    left to be rediscovered.
    """
    src = TICK.read_text()
    i = src.index('CAP_OUT="')
    block = src[i:i + 1800]
    code = "\n".join(ln for ln in block.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "cap_rc=$?" in code
    assert "capability-lost.json" in code
    # The `|| true` this issue is about must not come back on the CHECK itself.
    # The `grep ... || true` on the following line is fine and stays: grep exits
    # 1 when it matches nothing, which is the clean case.
    for ln in code.splitlines():
        if "check_no_capability_lost.py" in ln or "capability-lost.json" in ln:
            assert "|| true" not in ln, \
                "the capability check is back to report-only (vibeic-eda#88)"


def test_the_tick_still_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0


# --- the program itself must be able to fail, or the wiring above is decorative

def _cap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cap_lost", HERE / "check_no_capability_lost.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_probe_that_did_not_run_is_not_an_empty_loss_list():
    """The bug that made #88's wiring worth nothing on its own.

    `unresolvable` discarded `docker run`'s exit status, so an image that could
    not be started produced empty output, empty output meant "every name
    resolved", and the program printed `[PASS]` and exited 0. Measured against a
    tag that does not exist:

        before   0 no longer resolve ... [PASS]   rc=0
        after    [NOT CHECKED] could not probe ... rc=2

    Making the CALL SITE fail-closed while the program itself could not fail
    would have moved the defect, not fixed it.
    """
    C = _cap()
    C._sh = lambda *a, **k: (125, "", "Unable to find image")
    assert C.unresolvable("nope:nope", ["yosys", "klayout"]) is None, \
        "a failed probe still reads as 'nothing was lost'"


def test_a_probe_that_ran_still_reports_normally():
    """…and the other direction, or the fix is just 'always return None'."""
    C = _cap()
    C._sh = lambda *a, **k: (0, "klayout\n", "")
    assert C.unresolvable("img", ["yosys", "klayout"]) == ["klayout"]


def test_the_unprobeable_case_exits_two_and_says_nothing_was_compared(capsys):
    C = _cap()
    C.base_image = lambda df: "base:1"
    C.replaced_prefixes = lambda df: ["yosys"]
    C.command_names = lambda img, pre: ["yosys", "klayout"]
    C.unresolvable = lambda img, names: None
    rc = C.main([ "img:1", "--dockerfile", str(HERE.parent / "Dockerfile")])
    assert rc == C.RC_NOTHING
    assert "NOT CHECKED" in capsys.readouterr().err


def test_the_stale_json_is_overwritten_when_the_probe_fails(tmp_path):
    """Returning before the write leaves YESTERDAY's `"lost": []` on disk, and a
    consumer reading it sees a pass nothing produced today."""
    C = _cap()
    j = tmp_path / "cap.json"
    j.write_text('{"program": "check_no_capability_lost", "lost": []}\n')
    C.base_image = lambda df: "base:1"
    C.replaced_prefixes = lambda df: ["yosys"]
    C.command_names = lambda img, pre: ["yosys"]
    C.unresolvable = lambda img, names: None
    C.main(["img:1", "--dockerfile", str(HERE.parent / "Dockerfile"),
            "--json", str(j)])
    import json as _j
    got = _j.loads(j.read_text())
    assert got["lost"] is None and got.get("error"), got
