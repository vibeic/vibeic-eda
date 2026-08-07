#!/usr/bin/env python3
"""check_release_pins_committed.py — the review a release leaves outstanding.
vibeic-eda#99.

THE GAP THIS GUARDS. `daily_release.write_released_record` measures
`RELEASED.json`'s `pins` from the WORKING TREE, AFTER the pin edits it made.
`commit_release_record` then commits only `VERSION` / `RELEASED.json` /
`README.md` — never the pin files, by design (vibeic-eda#71,
`test_only_the_two_record_files_are_committed` in
`test_release_record_is_committed.py` is LOAD-BEARING and stays). So the
ordinary shape of a release is: `RELEASED.json` committed against pin B, the
tool Dockerfile still at pin A in HEAD, and the WORKING TREE holding pin B
(the edit nobody committed yet). Every pre-existing pin check —
`check_pins_agree`, `check_pin_descendants`, `check_release_recorded`'s own
`pins_moved` branch — reads the working tree and therefore reads clean.

THE LOAD-BEARING TEST is `test_a_publish_with_an_uncommitted_pin_is_caught`
below: it builds EXACTLY that state — RELEASED.json committed at the new pin,
the working tree ALSO at the new pin (so a filesystem-reading check would see
agreement), and HEAD still at the old one — and proves the round refuses
(RED), then commits the pin and proves it passes again (GREEN). A guard that
has never been seen to fail is worth nothing.

Every test drives the REAL script — either `main()` over a real git
repository built in `tmp_path`, or the underlying functions directly — no git
call is stubbed, because the entire point of this checker is which COPY of a
file it reads, and stubbing git is exactly the shortcut that would hide a
regression back to reading the working tree.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "check_release_pins_committed", HERE / "check_release_pins_committed.py")
C = importlib.util.module_from_spec(_spec)
sys.modules["check_release_pins_committed"] = C
_spec.loader.exec_module(C)                                        # type: ignore

SHA_A = "a" * 40
SHA_B = "b" * 40


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return p.stdout


def _dockerfile(sha: str) -> str:
    return (f"FROM scratch\n"
            f"# github.com/vibeic/OpenROAD\n"
            f"ARG OPENROAD_REF={sha}\n")


def _released(version: str, sha: str) -> str:
    return json.dumps({"version": version, "pins": {"OpenROAD": sha}}) + "\n"


def _repo(tmp_path: Path, name: str = "r") -> Path:
    """A repo with one pinned tool, `RELEASED.json` and the Dockerfile
    agreeing at SHA_A, both committed."""
    d = tmp_path / name
    (d / "tools" / "openroad").mkdir(parents=True)
    (d / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_A))
    (d / "RELEASED.json").write_text(_released("0.0.1-test", SHA_A))
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", "tools/openroad/Dockerfile", "RELEASED.json")
    _git(d, "commit", "-qm", "base")
    return d


def _run(repo: Path, *extra: str, capsys=None):
    argv = ["--eda-root", str(repo), *extra]
    rc = C.main(argv)
    out = capsys.readouterr() if capsys is not None else None
    return rc, out


# --- PASS -------------------------------------------------------------

def test_pass_when_committed_pins_match_released_json(tmp_path, capsys):
    repo = _repo(tmp_path)
    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_OK
    assert "[PASS]" in out.out
    assert "1 pin(s)" in out.out


# --- THE LOAD-BEARING RED/GREEN PROOF ----------------------------------

def test_a_publish_with_an_uncommitted_pin_is_caught(tmp_path, capsys):
    """RED: RELEASED.json committed at the NEW pin, the working tree ALSO
    edited to the new pin (mirroring exactly what `rewrite_pin` +
    `commit_release_record` leave behind), HEAD still stating the OLD one —
    the check must refuse. GREEN: commit the pin, and it must pass.
    """
    repo = _repo(tmp_path)

    # Simulate a publish: `daily_release.rewrite_pin` edits the Dockerfile in
    # the working tree (uncommitted); `commit_release_record` commits ONLY
    # VERSION/RELEASED.json/README.md, never the pin file.
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))
    (repo / "RELEASED.json").write_text(_released("0.0.2-test", SHA_B))
    _git(repo, "add", "RELEASED.json")
    _git(repo, "commit", "-qm", "release: record 0.0.2-test as published")

    # The working tree now AGREES with RELEASED.json — this is exactly the
    # state that made every pre-existing check read clean.
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert "tools/openroad/Dockerfile" in dirty, \
        "fixture bug: the pin edit must be uncommitted for this to test anything"

    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_OUTSTANDING, \
        f"a released pin is uncommitted at HEAD and the check did not refuse:\n{out.out}{out.err}"
    combined = out.out + out.err
    assert "OpenROAD" in combined
    assert SHA_B[:9] in combined, "the RECORDED (new) sha must be named"
    assert SHA_A[:9] in combined, "the HEAD (old, still-committed) sha must be named"
    assert "[FAIL]" in combined

    # GREEN: commit the pin — the outstanding review, done.
    _git(repo, "add", "tools/openroad/Dockerfile")
    _git(repo, "commit", "-qm", "pins: OpenROAD -> bbbbbbb")
    rc2, out2 = _run(repo, capsys=capsys)
    assert rc2 == C.RC_OK, f"still refusing after the pin was committed:\n{out2.out}{out2.err}"
    assert "[PASS]" in out2.out


def test_only_the_disagreeing_tool_is_named(tmp_path, capsys):
    """A second, agreeing tool must not appear in the finding — the point is
    to name exactly what is outstanding, not everything that was checked."""
    repo = _repo(tmp_path)
    (repo / "tools" / "yosys").mkdir(parents=True)
    yosys_sha = "c" * 40
    (repo / "tools" / "yosys" / "Dockerfile").write_text(
        f"FROM scratch\n# github.com/vibeic/yosys\nARG YOSYS_REF={yosys_sha}\n")
    rec = json.loads((repo / "RELEASED.json").read_text())
    rec["pins"]["yosys"] = yosys_sha
    (repo / "RELEASED.json").write_text(json.dumps(rec) + "\n")
    _git(repo, "add", "tools/yosys/Dockerfile", "RELEASED.json")
    _git(repo, "commit", "-qm", "add yosys, both pins agree")

    # Now only OpenROAD's pin goes stale (published but uncommitted).
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))
    rec["pins"]["OpenROAD"] = SHA_B
    (repo / "RELEASED.json").write_text(json.dumps(rec) + "\n")
    _git(repo, "add", "RELEASED.json")
    _git(repo, "commit", "-qm", "release: record as published")

    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_OUTSTANDING
    combined = out.out + out.err
    assert "OpenROAD" in combined
    assert "yosys" not in combined, "an agreeing pin must not be reported as outstanding"


# --- COULD-NOT-TELL, never a pass, never a named FAIL -------------------

def test_could_not_tell_when_released_json_is_absent(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "RELEASED.json").unlink()
    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_COULD_NOT_TELL
    assert rc not in (C.RC_OK, C.RC_OUTSTANDING)
    assert "COULD-NOT-TELL" in (out.out + out.err)


def test_could_not_tell_when_released_json_is_unparseable(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "RELEASED.json").write_text("not json at all")
    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_COULD_NOT_TELL
    assert "COULD-NOT-TELL" in (out.out + out.err)


def test_could_not_tell_when_released_json_has_no_pins_object(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "RELEASED.json").write_text(json.dumps({"version": "0.0.1-test"}) + "\n")
    rc, out = _run(repo, capsys=capsys)
    assert rc == C.RC_COULD_NOT_TELL


def test_could_not_tell_when_the_root_is_not_a_git_repo(tmp_path, capsys):
    d = tmp_path / "not-a-repo"
    d.mkdir()
    (d / "RELEASED.json").write_text(_released("0.0.1-test", SHA_A))
    rc, out = _run(d, capsys=capsys)
    assert rc == C.RC_COULD_NOT_TELL


# --- reads HEAD, never the working tree ----------------------------------

def test_pins_at_rev_ignores_an_uncommitted_edit(tmp_path):
    """Unit-level pin on the exact mechanism: `pins_at_rev` must come back
    with the COMMITTED sha even while the working tree holds a different one.
    """
    repo = _repo(tmp_path)
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))
    pins = C.pins_at_rev(repo, "HEAD")
    assert pins["OpenROAD"] == SHA_A, \
        "pins_at_rev read the working tree instead of the git object store"


def test_rev_argument_checks_an_arbitrary_revision(tmp_path, capsys):
    """`--rev` is not hardwired to HEAD — a caller (a pre-push gate, per the
    issue) must be able to ask about a branch tip that is not checked out."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "old-state")
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))
    (repo / "RELEASED.json").write_text(_released("0.0.2-test", SHA_B))
    _git(repo, "add", "tools/openroad/Dockerfile", "RELEASED.json")
    _git(repo, "commit", "-qm", "advance and commit the pin, on HEAD (main)")

    rc_head, _ = _run(repo, capsys=capsys)
    assert rc_head == C.RC_OK

    rc_old, out_old = _run(repo, "--rev", "old-state", capsys=capsys)
    assert rc_old == C.RC_OUTSTANDING, \
        "old-state's RELEASED.json (recorded at SHA_A) disagrees with " \
        "old-state's own tree, which never got the SHA_B pin at all"


# --- JSON sidecar ---------------------------------------------------------

def test_json_output_names_the_outstanding_tool(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "tools" / "openroad" / "Dockerfile").write_text(_dockerfile(SHA_B))
    (repo / "RELEASED.json").write_text(_released("0.0.2-test", SHA_B))
    _git(repo, "add", "RELEASED.json")
    _git(repo, "commit", "-qm", "release: record 0.0.2-test as published")

    j = tmp_path / "out.json"
    rc, _ = _run(repo, "--json", str(j), capsys=capsys)
    assert rc == C.RC_OUTSTANDING
    got = json.loads(j.read_text())
    assert got["verdict"] == "OUTSTANDING"
    assert got["outstanding"] == ["OpenROAD"]
    assert got["version"] == "0.0.2-test"


# --- exit-code sanity ------------------------------------------------------

def test_the_three_states_are_distinct_exit_codes():
    assert len({C.RC_OK, C.RC_OUTSTANDING, C.RC_COULD_NOT_TELL}) == 3
    assert C.RC_OK == 0, "0 must mean pass, matching every sibling check in this repo"


# --- WIRING (vibeic-eda#99, "the 05:30 tick is the unconditional answer") ---
#
# Source-inspection tests, matching the convention `test_run_tick_vars.py`
# already established for this exact file (`test_the_ci_finding_does_not_
# fail_the_tick`, `test_a_missing_checker_reports_rather_than_passing`). These
# do not execute run_tick.sh end-to-end (the capability-loss check earns that
# with a real subprocess harness in `test_capability_check_blocks_the_tick.py`
# because it once regressed silently under `|| true`; this checker has no such
# history yet and the source-level assertions below pin the same two facts
# that matter: it is CALLED, and it is NON-FATAL by design).

TICK = HERE / "run_tick.sh"


def test_the_tick_calls_the_new_checker():
    src = TICK.read_text()
    assert "check_release_pins_committed.py" in src


def test_the_tick_still_parses():
    import subprocess as sp
    assert sp.run(["bash", "-n", str(TICK)]).returncode == 0


def test_a_missing_checker_reports_rather_than_passing():
    src = TICK.read_text()
    i = src.index('RELPIN_OUT="')
    block = src[i:i + 1600]
    assert "MISSING:" in block
    assert "not a clean result" in block


def test_the_finding_does_not_fail_the_tick():
    """Deliberate, per the issue: 'log loudly, do not necessarily fail the
    tick' — the same treatment [claims]/[oracle]/[ci-ran] get, because closing
    the gap is a human review this tick cannot perform for itself without
    reintroducing the #71 defect (committing an unreviewed pin move).
    """
    src = TICK.read_text()
    i = src.index('RELPIN_OUT="')
    block = src[i:i + 1600]
    assert "relpin_rc=$?" in block
    # …and the tick's OWN exit-code ladder, at the bottom of the file, must
    # never fold this rc in. Anchored on the ladder's actual assignment line
    # (`[ "${guard_rc}" != "0" ] && [ "${rc}" = "0" ] && rc=3`), not on the
    # earlier `if [ "${guard_rc}" != "0" ]; then` right after the source-guard
    # loop — that phrase appears first and a naive anchor on it would swallow
    # this very block (which legitimately mentions `relpin_rc` in its own
    # wiring) into the "ladder" slice and make the assertion pass for the
    # wrong reason. Not a line count either, for the same reason RELPIN_OUT's
    # own comment gives: a fragile offset is how a future insertion between
    # the two goes unnoticed.
    ladder_start = src.index('[ "${guard_rc}" != "0" ] && [ "${rc}" = "0" ]')
    ladder = src[ladder_start:]
    assert "relpin_rc" not in ladder, \
        "release-pins is folded into the tick's exit code — it must stay " \
        "report-only, matching [claims]/[oracle]/[ci-ran]"


def test_the_call_site_captures_the_status_on_the_same_line():
    """The `[ship]` block's own rule: a status read on the line AFTER a pipe
    reads the WRONG process's exit code. `head`/`grep`/`tail` piped from the
    python3 call would silently break this if the capture moved off the call
    site's own line."""
    src = TICK.read_text()
    i = src.index('RELPIN_OUT="')
    block = src[i:i + 400]
    assert "relpin_rc=$?" in block
    lines = block.splitlines()
    call_i = next(n for n, ln in enumerate(lines)
                  if "check_release_pins_committed.py" in ln and "python3" in ln)
    # the assignment is the next non-continuation line after the call (the
    # call itself may wrap across a `\` line-continuation, as it does here)
    tail = "\n".join(lines[call_i:call_i + 4])
    assert "relpin_rc=$?" in tail


def test_json_sidecar_path_is_wired():
    src = TICK.read_text()
    i = src.index('RELPIN_OUT="')
    block = src[i:i + 1600]
    assert "release-pins-committed.json" in block


def test_every_checker_in_this_directory_is_called_by_something():
    """The sweep `test_run_tick_vars.py::test_every_checker_in_this_directory_
    is_called_by_something` already runs over every `.py` file in this
    directory — this just pins that the new one does not need adding to that
    test's `NOT_A_GATE` allow-list, i.e. that the wiring above is real and not
    merely present in a comment."""
    src = TICK.read_text()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "check_release_pins_committed.py" in code, \
        "the reference above is in a comment only — the sweep test would " \
        "still flag this file as unwired"
