#!/usr/bin/env python3
"""Merge ALL new upstream commits into every fork we build. Every day. No PR.

OWNER RULING, 2026-07-29 (two statements, both binding):
  * "daily merge all new commit from upstream for forked tools"
  * "每天自動合併、不等人" — merge automatically, do not wait for a human.

This supersedes the selective-adoption path (`GK_MERGE_PR`), which opens a
cherry-pick PR for the commits an LLM judged clearly-safe and leaves everything
else for a person. That path is still correct for what it does and is not
removed; it is no longer the thing that decides whether we are current.

WHAT THIS DOES DIFFERENTLY
==========================
Selective adoption asks "should we take this commit?", one commit at a time,
and its default answer is "ask a human". Measured the day the ruling landed:
9 assessments existed, 5 commits had ever been auto-adopted, 35 were queued for
a human, and the fleet was 1229 commits behind — because the projects that
matter most (OpenROAD, yosys, verilator, iverilog, ngspice) do not tag releases
at all, so they had never even entered the queue.

This takes the whole upstream branch. `git merge`, not cherry-pick. The fork's
own commits are carried by the merge and verified afterwards.

WHAT IT REFUSES TO DO
=====================
A conflict is NOT resolved automatically. Merging everything is a policy the
owner set; guessing what a conflicting hunk should say is not, and a wrong
resolution compiles. On conflict this ABORTS the merge, leaves the fork
untouched, and reports the file list — that fork stays behind until a human
resolves it, and the report says so rather than reporting success.

It never publishes a non-fast-forward candidate, never touches a build branch
it did not start from, and never runs in the shared checkout's working tree.
Every publication also carries an exact expected-old-tip lease: a concurrent
advance *or rewind* of the remote branch rejects the push instead of being
silently overwritten.

POST-MERGE CHECKS (vibeic-eda#89)
=================================
A CLEAN MERGE IS NOT A CORRECT MERGE. `git merge` refuses only on TEXTUAL
conflict — two sides editing the same lines. A whole class of breakage arrives
with no conflict at all, because the two sides edit different files and only
disagree about something no diff can see.

Measured, on one warning, three times in six days:

  515 -> 519   2f9fbcd47e   upstream took RCX 515 in multiChipExtractor.cpp
  519 -> 524   5bb6ca31ee   upstream took RCX 519 in OpenRCX.tcl
  524 -> 527   724a389026   upstream took RCX 524 in ext.i

Every one of those is our resistance-clamp warning in `extmain_v2.cpp` landing
on a logger message id upstream had grown into. Every one merged with ZERO
conflicting files, so git took it silently, and every one was found afterwards
by hand. Upstream is growing the RCX id range from below; this is a recurring
generator, not three incidents.

So a fork may DECLARE, in `FORKS.json`, commands that must pass on the merged
tree before it is published:

    { "tool": "OpenROAD", "upstream": "...",
      "post_merge_check": [
        { "name": "dup-logger-ids",
          "path": "etc/find_messages.py",
          "cmd": ["python3", "etc/find_messages.py", "-d", "src"],
          "why": "..." } ] }

They run in the merge worktree, AFTER the merge succeeds and BEFORE the push.
A non-zero exit is recorded as `POST_MERGE_CHECK_FAILED`, nothing is pushed, and
the fork is left at its previous tip — the same outcome a textual conflict gets,
because it is the same situation: an automatic merge this program is not
entitled to publish.

Three things that are deliberately NOT passes:
  * a declared check whose `path` is absent from the merged tree,
  * a check that could not be executed, or stopped making forward progress,
  * an entry that is malformed.
Each is a merge this program could not verify, and an unverified merge is not a
verified one. They fail exactly like a check that ran and found something.

Exit: 0 every fork current or already current, 1 at least one needs a human,
      2 nothing was attempted (which is not success).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import progress_watchdog as watchdog

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: Where the fork checkouts live. Each is a SHARED tree that other sessions and
#: this tick's siblings use, so every operation here runs in a worktree of it and
#: never in the tree itself.
FORKS_ROOT = Path(os.environ.get("VIBEIC_FORKS_ROOT", "/home/reyerchu/vibe-ic-forks"))
GIT_STALL_GRACE = float(os.environ.get("GK_GIT_STALL_GRACE", "1800"))
GIT_PROGRESS_POLL_S = float(os.environ.get("GK_GIT_PROGRESS_POLL", "5"))


def _sh(args: List[str], cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
        *, progress: bool = False, stall_grace: Optional[float] = None):
    if progress:
        run = watchdog.run_supervised(
            args, cwd=cwd,
            stall_grace_s=(GIT_STALL_GRACE if stall_grace is None else stall_grace),
            poll_s=GIT_PROGRESS_POLL_S,
            output_progress=True,
            process_tree_cpu_progress=True,
        )
        return run.rc, run.out, run.err
    try:
        kwargs = {
            "cwd": str(cwd) if cwd else None,
            "capture_output": True,
            "text": True,
        }
        # Only callers that are explicitly single-request API probes set this.
        # Local metadata commands have no elapsed-time correctness verdict;
        # long clone/fetch/worktree/merge/push paths use the progress branch.
        if timeout is not None:
            kwargs["timeout"] = timeout
        p = subprocess.run(args, **kwargs)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


#: vibeic repo -> upstream slug, for the sources that are mirrors rather than
#: forks. Kept small and explicit: every entry is a repo the image clones, and
#: adding one by hand is the price of the fork API refusing us.
MIRROR_UPSTREAMS = {
    "kissat": "arminbiere/kissat",
    "cadical": "arminbiere/cadical",
    "ASAP7_for_KLayout": "laurentc2/ASAP7_for_KLayout",
    "asap7_pdk_r1p7": "The-OpenROAD-Project/asap7_pdk_r1p7",
    "asap7sc7p5t_28": "The-OpenROAD-Project/asap7sc7p5t_28",
    "OpenROAD-flow-scripts": "The-OpenROAD-Project/OpenROAD-flow-scripts",
}


#: How many times a TRANSPORT failure on the live-HEAD probe is re-asked, and
#: how long to wait between attempts.  Small on purpose: this is a blip filter,
#: not a way to sit on an outage.
LIVE_HEAD_ATTEMPTS = 3
LIVE_HEAD_RETRY_SLEEP_S = 5.0
LIVE_HEAD_TIMEOUT_S = 120

#: A transport failure says NOTHING about the remote's answer, so re-asking is
#: the same measurement, not a softer one.  Matched on the shapes `_sh` and git
#: actually produce (`_sh` returns rc=127 with "TimeoutExpired: ..." for its own
#: deadline; git prints these for a refused/reset/unreachable transport).
_TRANSPORT_FAILURE_RE = re.compile(
    r"TimeoutExpired|Connection (?:timed out|reset|refused)|"
    r"Could not resolve host|Temporary failure in name resolution|"
    r"unable to access|The remote end hung up|early EOF|"
    r"gnutls_handshake\(\) failed|SSL_ERROR_SYSCALL|Operation timed out|"
    r"HTTP/2 stream .* was not closed cleanly|RPC failed",
    re.I)


def _is_transport_failure(rc: int, out: str, err: str) -> bool:
    """Did the question fail to REACH the remote, as opposed to being answered?

    Only a non-zero rc can be a transport failure.  An rc of 0 means git spoke
    to the remote and printed what it got; if that output is ambiguous or
    malformed, that is a finding ABOUT THE REMOTE and re-asking it would be
    asking a different question until it gives a nicer answer.
    """
    if rc == 0:
        return False
    if rc == watchdog.RC_STALLED:
        return True
    return bool(_TRANSPORT_FAILURE_RE.search(f"{out}\n{err}"))


def _live_head_with_transport_retry(src: Path):
    """`ls-remote --symref upstream HEAD`, re-asked when the TRANSPORT failed.

    vibeic-eda: on 2026-09-08 the 05:30 round reported
    `yosys UPSTREAM_DEFAULT_UNMEASURABLE ... (rc=127: TimeoutExpired: ...
    timed out after 120 seconds)`.  One fork unmeasured makes daily_merge
    NEEDS HUMAN, which makes the blocking gate REFUSE daily_release, so a
    single network blip cost the whole day's release.  The same command,
    retried by hand four times minutes later, answered in 0.7 s every time.

    Refusing on an unmeasured fork is CORRECT and is not what changed.  What
    changed is that a failure to reach the remote is now re-asked before it is
    called a measurement: the retry runs only for transport failures, keeps the
    same command, the same 120 s deadline and the same parsing, and every
    attempt is returned in the diagnostic so a real outage still refuses -- with
    all three failures visible, not just the last.
    """
    attempts = []
    for i in range(LIVE_HEAD_ATTEMPTS):
        rc, out, err = _sh([
            "git", "-C", str(src), "ls-remote", "--symref", "upstream", "HEAD",
        ], timeout=LIVE_HEAD_TIMEOUT_S)
        if rc == 0:
            return rc, out, err
        attempts.append(f"attempt {i + 1}/{LIVE_HEAD_ATTEMPTS} rc={rc}: "
                        + " ".join((err or out).split())[:160])
        if not _is_transport_failure(rc, out, err):
            break
        if i + 1 < LIVE_HEAD_ATTEMPTS:
            time.sleep(LIVE_HEAD_RETRY_SLEEP_S)
    return rc, out, err + ("\n" if err else "") + "; ".join(attempts)


def _upstream_url(repo: str) -> str:
    """Upstream of a vibeic fork, asked of GitHub rather than hard-coded."""
    rc, out, _ = _sh(["gh", "api", f"repos/vibeic/{repo}",
                      "--jq", ".parent.full_name // empty"], timeout=120)
    slug = out.strip() if rc == 0 else ""
    # `gh api` prints the ERROR BODY to stdout on failure, and `--jq` leaves it
    # there when the document has no such field. Without the rc check a missing
    # repo produced
    #     https://github.com/{"message":"Not Found",...}.git
    # — a URL built out of an error message, which git would then fail to clone
    # for a reason with nothing to do with the actual problem. Found by probing
    # a repo that does not exist, which is the case nobody writes a test for.
    if not slug or "/" not in slug or slug.startswith("{"):
        slug = ""
    if not slug:
        # NINE sources are PUSH MIRRORS, not GitHub forks: the fork API answered
        # 403 for all of them, so they were created as repos and pushed. GitHub
        # records no `parent` for those, and a program that reads the parent
        # therefore sees nothing. The upstream is still a fact — it is just a
        # fact GitHub is not holding for us, so we hold it rather than reporting
        # the mirror as unsurveyable.
        #
        # FORKS.json FIRST, the hard-coded map second. vibeic-eda#30 mirrored
        # open_pdks, IHP-Open-PDK and ciel and declared all three there — and
        # all three still reported NO_UPSTREAM, because the map below was not
        # extended and nothing required it to be. A declaration that one program
        # reads and another ignores is how #30 landed without doing its job.
        # Reading the declaration means the next mirror works with no edit here.
        slug = _declared_upstream(repo) or MIRROR_UPSTREAMS.get(repo, "")
    return f"https://github.com/{slug}.git" if slug else ""


@functools.lru_cache(maxsize=1)
def _declared_upstreams() -> Dict[str, str]:
    """tool -> upstream slug, from FORKS.json. Empty when it cannot be read."""
    f = Path(__file__).resolve().parent / "FORKS.json"
    try:
        return {x["tool"]: x["upstream"] for x in
                json.loads(f.read_text())["forks"] if x.get("upstream")}
    except (OSError, ValueError, KeyError):
        return {}


def _declared_upstream(repo: str) -> str:
    return _declared_upstreams().get(repo, "")


def _declared_branch(repo: str) -> Optional[str]:
    """The one build line this fork is declared to publish from.

    A commit may be reachable from many branches.  Reachability therefore
    proves that a branch *can* build the pin, not that it is the line we intend
    to advance.  Keep that identity next to the fork's upstream declaration so
    every consumer reads the same answer.
    """
    for entry in _forks_json().get("forks", []):
        if not isinstance(entry, dict) or entry.get("tool") != repo:
            continue
        branch = entry.get("branch")
        if not isinstance(branch, str) or not branch:
            return None
        if (branch != branch.strip() or any(ch.isspace() for ch in branch)
                or branch.startswith("-") or branch.startswith("/")
                or branch.endswith(("/", ".")) or ".." in branch
                or any(ch in branch for ch in "~^:?*[\\")):
            return None
        return branch
    return None


#: How long a post-merge check may make ZERO observable progress.  This is not
#: a runtime budget: output or process-tree CPU advancement renews the lease,
#: and a progressing check may run indefinitely.  The name deliberately does
#: not retain the old ``*_TIMEOUT`` lie; elapsed time is not a merge verdict.
POST_MERGE_STALL_GRACE = float(
    os.environ.get("GK_POST_MERGE_STALL_GRACE", "1800")
)
POST_MERGE_POLL_S = float(os.environ.get("GK_POST_MERGE_POLL_S", "5"))


def _forks_json(path: Optional[Path] = None) -> dict:
    f = path or (Path(__file__).resolve().parent / "FORKS.json")
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def post_merge_checks(repo: str, forks_json: Optional[Path] = None) -> List[dict]:
    """The checks `repo` declares in FORKS.json, or [] if it declares none.

    NO declaration is a real, allowed answer: most of these 36 sources have no
    invariant a merge can silently break, and inventing a default check for them
    would be noise. What is NOT allowed is a declaration that is present and
    unreadable — that is handled where the checks run, not by dropping it here.
    """
    for entry in _forks_json(forks_json).get("forks", []):
        if entry.get("tool") == repo:
            decl = entry.get("post_merge_check") or []
            return decl if isinstance(decl, list) else [{"_malformed": decl}]
    return []


def _is_pytest_command(cmd: List[str]) -> bool:
    """Whether a declaration invokes pytest and needs semantic supervision."""
    if not cmd:
        return False
    executable = Path(cmd[0]).name
    return (executable.startswith("pytest")
            or any(cmd[i:i + 2] == ["-m", "pytest"]
                   for i in range(1, len(cmd) - 1)))


def _semantic_pytest_run(cmd: List[str], wt: Path) -> tuple[int, str, str, dict]:
    """Run a pytest declaration through the same semantic lease as 05:30.

    The semantic driver contains its own progress supervisor. This parent wait
    intentionally has no timeout: wrapping it in another elapsed-time or
    output-activity watchdog would let the weaker outer signal overrule the
    validated pytest lifecycle channel.
    """
    with tempfile.TemporaryDirectory(prefix="vibeic-eda-postmerge-pytest-") as td:
        root = Path(td)
        record_path = root / "record.json"
        argv = [
            sys.executable,
            str(Path(__file__).resolve().parent / "semantic_pytest_gate.py"),
            "--junit", str(root / "junit.xml"),
            "--json", str(record_path),
            "--cwd", str(wt),
            "--stall-grace", str(POST_MERGE_STALL_GRACE),
            "--poll", str(POST_MERGE_POLL_S),
            "--", *cmd,
        ]
        try:
            proc = subprocess.run(
                argv, cwd=str(wt), capture_output=True, text=True,
            )
        except OSError as exc:
            return 127, "", f"COMMAND_NOT_STARTED: {type(exc).__name__}: {exc}", {
                "outcome": "launch_error",
                "elapsed_seconds": 0.0,
                "mechanism": "validated_semantic_progress_lease",
            }
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {
                "outcome": "protocol_incomplete",
                "elapsed_seconds": 0.0,
                "mechanism": "validated_semantic_progress_lease",
                "reason": "semantic pytest driver wrote no readable record",
            }
        outcome = str(record.get("outcome") or "protocol_incomplete")
        if proc.returncode == 2 and outcome == "natural":
            outcome = "protocol_incomplete"
        record["outcome"] = outcome
        return proc.returncode, proc.stdout, proc.stderr, record


def run_post_merge_checks(wt: Path, checks: List[dict]) -> List[dict]:
    """Run each declared check in the merged worktree. Returns one row per check.

    A row is {name, ok, rc, detail}. `ok` False means DO NOT PUBLISH — and it is
    False for every way of not getting a clean answer, not only for a check that
    ran and found something. The three that bit this fleet elsewhere:

      * the script is not in the tree (upstream moved or deleted it) — that is a
        check nobody ran, and "MISSING is not a pass" is this repo's oldest rule;
      * the process could not be started or made no forward progress — measuring
        nothing proves nothing, the same reasoning `capability_gate` uses for
        its rc=2;
      * the declaration is malformed — a typo in FORKS.json must not read as
        "this fork declares no checks", because that is silently identical to
        deleting the gate.
    """
    rows: List[dict] = []
    for chk in checks:
        name = str(chk.get("name") or chk.get("path") or "unnamed")
        cmd = chk.get("cmd")
        rel = chk.get("path")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd) \
                or not isinstance(rel, str) or not rel:
            rows.append({"name": name, "ok": False, "rc": -1,
                         "outcome": "malformed", "elapsed_seconds": 0.0,
                         "detail": "MALFORMED declaration in FORKS.json — needs a "
                                   "string `path` and a non-empty list-of-strings "
                                   "`cmd`; a check that cannot be read is not a "
                                   "check that passed"})
            continue
        target = wt / rel
        if not target.exists():
            rows.append({"name": name, "ok": False, "rc": -1,
                         "outcome": "missing", "elapsed_seconds": 0.0,
                         "detail": f"MISSING: {rel} is not in the merged tree — "
                                   f"nothing was checked, which is not a clean result"})
            continue
        if _is_pytest_command(cmd):
            rc, out, err, supervision = _semantic_pytest_run(cmd, wt)
            outcome = str(supervision.get("outcome") or "protocol_incomplete")
            elapsed = float(supervision.get("elapsed_seconds") or 0.0)
            progress_basis = "validated_pytest_lifecycle"
        else:
            run = watchdog.run_supervised(
                cmd, cwd=wt, stall_grace_s=POST_MERGE_STALL_GRACE,
                poll_s=POST_MERGE_POLL_S, process_tree_cpu_progress=True,
            )
            rc, out, err = run.rc, run.out, run.err
            outcome = run.outcome
            elapsed = run.elapsed_s
            progress_basis = "output_bytes_or_process_tree_cpu"
            supervision = {
                "mechanism": "activity_progress_lease",
                "outcome": outcome,
                "elapsed_seconds": round(elapsed, 3),
                "stall_grace_seconds": POST_MERGE_STALL_GRACE,
                "total_runtime_ceiling_seconds": None,
            }
        # launch_error and stalled are evidence states of their own.  Neither is
        # collapsed into the command's natural non-zero verdict, and neither can
        # become clean merely because a partial stdout fragment looked healthy.
        #
        # `advisory` — a check that does NOT read the merged tree cannot tell us
        # anything about the merge, so it is not entitled to block the publish.
        # klayout's `vendored-plugin-sync` is the case that forced this: it
        # compares the fork against a vibe-ic PLUGIN checkout located by
        # `~/vibe-ic/...` hints, i.e. ANOTHER REPO'S WORKING COPY on this host.
        # Merging upstream into klayout cannot make that copy more or less
        # stale, and on a host with no plugin checkout the script exits SKIP=3,
        # which is non-zero — so wired as a hard gate it blocks klayout's
        # upstream sync FOREVER, on every host, for a defect in a different
        # repository. Measured 2026-08-13: `klayout upstream: already current;
        # BLOCKED by post-merge check: vendored-plugin-sync rc=1 — NOT pushed`.
        #
        # An advisory check still RUNS, still reports, and is still red in the
        # round's self-check summary and on the page. The only thing it loses is
        # the veto over publishing this fork. It must never be used to quiet a
        # check that DOES read the merged tree — that is the whole gate.
        advisory = bool(chk.get("advisory"))
        rows.append({"name": name, "ok": rc == 0 or advisory, "rc": rc,
                     "advisory": advisory,
                     "outcome": outcome,
                     "elapsed_seconds": round(elapsed, 3),
                     "progress_basis": progress_basis,
                     "supervision": supervision,
                     "detail": ("clean" if rc == 0 else
                                ("ADVISORY (does not read the merged tree; "
                                 "reported, not blocking) " if advisory else "")
                                + (_salient(out, err) if rc != 0 else ""))})
    return rows


#: Markers that NAME a defect, as opposed to lines that merely sit at the end.
#: Deliberately not a word list — "ERROR" matched 8 rows of `find_messages.py`'s
#: 3000-line message INVENTORY, every one a message that exists on purpose.
_DEFECT_MARKERS = (
    "Error: ",          # find_messages.py — "<SET> <id> used N times"
    "Appears in ",      # find_messages.py — where the collision is
    "FAIL:",            # check_test_registration_parity.py
    "UNEXPECTED",
    "STALE ALLOWLIST",
    "integration ",     # its per-row output
    "cpp-unit ",
)


def _salient(out: str, err: str) -> str:
    """The lines that say WHAT BROKE, not the last four lines of the output.

    `tail` is wrong here for the reason `prepush-gates.sh` already documents: an
    aggregating check puts its failure in the middle and its ADVICE at the end.
    Measured on `check_test_registration_parity.py` with one test un-wired — the
    last four lines are "Wire each into its module's BUILD ... bazel sandboxes,
    and an undeclared data file fails the test", which is true, generic, and
    names neither the module nor the test. `tap:bound_to_placement` was four
    lines above it.

    Streams are searched SEPARATELY and stderr first, because the two checks
    this fleet declares put their verdict in opposite places: find_messages.py
    prints its whole inventory to stdout and the collision to stderr, so a
    merged search is a haystack that does not contain the needle.
    """
    for stream in (err, out):
        lines = (stream or "").splitlines()
        hit = [ln.strip() for ln in lines
               if any(k in ln for k in _DEFECT_MARKERS)]
        if hit:
            return " | ".join(hit[:8])[:400]
    tail = ((err or "").strip() or (out or "").strip()).splitlines()
    # No recognised marker: say so rather than presenting a guess as a finding.
    return ("(no recognised marker; last lines) "
            + " | ".join(tail[-4:]))[:400]


def build_branches(eda_root: Path) -> Dict[str, str]:
    """fork repo -> the pinned commit, or "" for a source we track without a pin.

    Read rather than configured: a branch list is a second copy of a value the
    Dockerfiles already state, and the two drift the moment someone repoints a
    build without updating the list.

    A PIN IS NOT THE ONLY REASON TO WATCH A SOURCE, and assuming it was made
    vibeic-eda#30 land without doing its job. That change mirrored open_pdks,
    IHP-Open-PDK and ciel so the daily upstream check would watch them, and
    declared all three in FORKS.json — and this survey kept reporting 18 of 30,
    because it enumerates from `ARG <NAME>_REF=<40 hex>` and those three have no
    such ARG: the Dockerfile clones the PDKs by URL, and the PDKs themselves
    arrive in the base image.

    So the mirrors existed, the declaration existed, and nothing looked at them
    — the exact gap #30 was opened to close, surviving the change that closed
    it. FORKS.json is now the second source: a declared source with no pin is
    surveyed with pin "", and `branch_for` falls back to the fork's default
    branch. The upstream side is the value for those; there is no pin to report
    and that is correct rather than missing.
    """
    out: Dict[str, str] = {}
    for df in sorted((eda_root / "tools").glob("*/Dockerfile")) + [eda_root / "Dockerfile"]:
        if not df.is_file():
            continue
        text = df.read_text(errors="replace")
        for m in re.finditer(
                r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text):
            repo = m.group(1)
            arg = repo.upper().replace("-", "_")
            line = re.search(rf"^ARG {arg}_REF=([0-9a-f]{{40}}).*$", text, re.M)
            if line:
                out.setdefault(repo, line.group(1))

    declared = eda_root / "fork-gatekeeper" / "FORKS.json"
    if declared.is_file():
        try:
            for f in json.loads(declared.read_text())["forks"]:
                out.setdefault(f["tool"], "")
        except (ValueError, KeyError) as exc:                  # noqa: BLE001
            print(f"[warn] {declared} unreadable ({exc}) — surveying pinned "
                  f"sources only, which is fewer than we track", file=sys.stderr)
    return out


BRANCH_FOUND = "found"
BRANCH_ABSENT = "absent"
BRANCH_UNKNOWN = "unknown"


class BranchLookup:
    """One authoritative branch-at-pin observation.

    ``branch=None`` used to collapse two opposite facts: GitHub successfully
    listed every branch and found no match, or GitHub never answered.  A merge
    gate cannot turn the latter into the former, so callers consume ``status``
    and retain it in the result row.
    """

    __slots__ = ("status", "branch", "detail", "rc")

    def __init__(self, status: str, branch: Optional[str] = None,
                 detail: str = "", rc: Optional[int] = None):
        self.status = status
        self.branch = branch
        self.detail = detail
        self.rc = rc

    def __repr__(self) -> str:
        return (f"BranchLookup(status={self.status!r}, branch={self.branch!r}, "
                f"detail={self.detail!r}, rc={self.rc!r})")


def _branch_lookup_error(repo: str, rc: int, out: str, err: str) -> str:
    text = " ".join((err or out).split())
    if len(text) > 220:
        text = text[:108] + " ... " + text[-108:]
    return (f"GitHub branch lookup for vibeic/{repo} returned rc={rc}"
            + (f": {text}" if text else " with no diagnostic"))


def branch_at_pin(repo: str, pin: str) -> BranchLookup:
    """Verify the declared build branch; never infer identity from ancestry.

    Several branches may contain the image pin.  Comparing all of them and
    selecting the first name that looks durable can therefore merge and push a
    perfectly real commit onto the wrong line.  ``FORKS.json`` declares the one
    line we publish.  The branch endpoint proves that line currently exists;
    one compare then proves whether it contains the pin.

    A unique exact-tip match remains only as compatibility for synthetic/old
    pinned registries with no declaration.  It never searches descendants:
    zero or multiple exact matches are UNKNOWN, not permission to guess.
    """
    # --paginate + per_page is load-bearing. The unpaginated endpoint returns
    # the first 30 branches, which previously made real build lines disappear.
    rc, out, err = _sh(["gh", "api", "--paginate",
                        f"repos/vibeic/{repo}/branches?per_page=100",
                        "--jq", ".[] | .name + \" \" + .commit.sha"], timeout=180)
    if rc != 0:
        return BranchLookup(
            BRANCH_UNKNOWN, detail=_branch_lookup_error(repo, rc, out, err), rc=rc,
        )

    rows = [ln.split() for ln in out.splitlines() if ln.strip()]
    if (any(len(row) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", row[1])
            for row in rows)
            or len({row[0] for row in rows}) != len(rows)):
        return BranchLookup(
            BRANCH_UNKNOWN,
            detail="GitHub branch listing completed but its output was malformed",
            rc=rc,
        )

    declared = _declared_branch(repo)
    if not declared:
        if not pin:
            return BranchLookup(
                BRANCH_UNKNOWN,
                detail=(f"FORKS.json has no valid branch declaration for "
                        f"unpinned source {repo}"),
                rc=0,
            )
        exact = [row[0] for row in rows if row[1].lower() == pin.lower()]
        if len(exact) == 1:
            return BranchLookup(
                BRANCH_FOUND, branch=exact[0],
                detail=(f"compatibility fallback: the sole exact-tip branch "
                        f"{exact[0]} is at pin {pin[:9]}; declare it in "
                        "FORKS.json"),
                rc=0,
            )
        why = ("no exact-tip branch" if not exact
               else f"{len(exact)} exact-tip branches")
        return BranchLookup(
            BRANCH_UNKNOWN,
            detail=(f"FORKS.json has no valid branch declaration for {repo}; "
                    f"{why} at pin {pin[:9]}, so branch identity is unknowable"),
            rc=0,
        )

    tips = {row[0]: row[1] for row in rows}
    if declared not in tips:
        return BranchLookup(
            BRANCH_ABSENT,
            detail=(f"GitHub authoritatively listed every branch, but declared "
                    f"build branch {declared!r} does not exist"),
            rc=0,
        )
    if not pin:
        return BranchLookup(
            BRANCH_FOUND, branch=declared,
            detail=f"GitHub confirmed declared build branch {declared}", rc=0,
        )

    endpoint = (f"repos/vibeic/{repo}/compare/"
                f"{quote(pin, safe='')}...{quote(declared, safe='')}")
    crc, cout, cerr = _sh(["gh", "api", endpoint], timeout=120)
    if crc != 0:
        diagnostic = " ".join((cerr or cout).split())
        return BranchLookup(
            BRANCH_UNKNOWN,
            detail=(f"compare pin...{declared} returned rc={crc}"
                    + (f": {diagnostic[:220]}" if diagnostic else "")),
            rc=crc,
        )
    try:
        payload = json.loads(cout)
    except (TypeError, ValueError):
        payload = None
    status = payload.get("status") if isinstance(payload, dict) else None
    if status in {"ahead", "identical"}:
        return BranchLookup(
            BRANCH_FOUND, branch=declared,
            detail=(f"GitHub compare confirmed declared build branch {declared} "
                    f"contains pin {pin[:9]} (status={status})"),
            rc=0,
        )
    if status in {"behind", "diverged"}:
        return BranchLookup(
            BRANCH_ABSENT,
            detail=(f"GitHub compare confirmed declared build branch {declared} "
                    f"does not contain pin {pin[:9]} (status={status})"),
            rc=0,
        )
    return BranchLookup(
        BRANCH_UNKNOWN,
        detail=f"compare pin...{declared} returned no valid status",
        rc=0,
    )


def branch_for(repo: str, pin: str) -> Optional[str]:
    """Compatibility view for callers that only need a confirmed branch.

    The landing gate itself uses :func:`branch_at_pin`; this lossy view must
    never be used to form an absent-versus-unknown verdict.
    """
    return branch_at_pin(repo, pin).branch


def merge_one(repo: str, branch: str, dry: bool = False,
              checks: Optional[List[dict]] = None,
              expected_pin: str = "") -> dict:
    """Merge upstream's default branch into `branch`, in an isolated worktree.

    `checks` are the post-merge checks to run on the merged tree before pushing;
    None means "look up what this fork declares in FORKS.json".  `expected_pin`
    is the pin whose membership in the declared branch was proved during lookup;
    it is re-proved after the fresh fetch so a force-move cannot redirect this
    merge between observation and action.
    """
    src = FORKS_ROOT / repo
    if checks is None:
        checks = post_merge_checks(repo)
    res = {"repo": repo, "branch": branch, "state": "?", "detail": "",
           "conflicts": [], "checks": [], "took": 0}
    if not (src / ".git").exists() and not (src / "HEAD").exists():
        # A fork with no local checkout is not a fork with nothing to merge.
        # Newly-created mirrors land here on their first tick; clone rather than
        # report a state that reads like "nothing to do".
        src.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = _sh(["git", "clone", "--quiet",
                          f"https://github.com/vibeic/{repo}.git", str(src)],
                         progress=True)
        if rc != 0:
            res.update(state=("CLONE_STALLED" if rc == watchdog.RC_STALLED
                              else "CLONE_FAILED"), detail=err.strip()[:200])
            return res
        _sh(["git", "-C", str(src), "remote", "add", "upstream",
             _upstream_url(repo)])

    # THE SHARED TREE IS READ-ONLY TO US. Its working tree may hold someone
    # else's uncommitted work — measured: the OpenROAD checkout carried five
    # uncommitted files belonging to another session on the day this was written.
    before = _sh(["git", "status", "--porcelain"], src)[1]

    rc, out, err = _sh(["git", "-C", str(src), "remote", "get-url", "upstream"])
    if rc != 0 or not out.strip():
        # A freshly-mirrored fork has no `upstream` remote. Configure it from
        # what GitHub says the parent is, rather than refusing: "no upstream
        # configured" is a fact about our clone, not about the fork.
        url = _upstream_url(repo)
        if not url:
            res.update(state="NO_UPSTREAM",
                       detail="GitHub records no parent for this repo — a push "
                              "mirror, not a fork; upstream must be set by hand")
            return res
        # `add` FAILS when the remote exists with an empty URL, which is what a
        # mirror's checkout looks like — and the failure surfaced one step later
        # as `git fetch upstream` -> "fatal: no path specified", a message about
        # a URL rather than about the missing parent it actually was.
        rc_add, _, _ = _sh(["git", "-C", str(src), "remote", "add",
                            "upstream", url])
        if rc_add != 0:
            _sh(["git", "-C", str(src), "remote", "set-url", "upstream", url])
        out = url
    tmp = Path(tempfile.mkdtemp(prefix=f"daily_merge_{repo}_"))
    wt = tmp / "wt"
    try:
        # origin FIRST. The build branch is resolved from the fork over the API,
        # so a stale remote-tracking ref makes `origin/<branch>` an invalid
        # reference for a branch that plainly exists — measured on the first
        # dry-run, which reported WORKTREE_FAILED for six forks that were fine.
        # WIDEN THE REFSPEC FIRST. Six of these checkouts were cloned
        # `--single-branch`, so `remote.origin.fetch` names ONE branch and
        # `git fetch origin` never brings any other. `origin/<build-branch>` is
        # then an invalid reference for a branch that plainly exists on the fork
        # — which is what six WORKTREE_FAILED rows were, and reading them as
        # "the branch is missing" would have been exactly backwards.
        #
        # It also makes local counts lie: OpenROAD kept a stale
        # `origin/vibeic/openroad-integration` from an older clone that fetch
        # never updated, and measuring against it reported 791 commits behind
        # upstream for a branch that had already been merged.
        _sh(["git", "-C", str(src), "remote", "set-branches", "origin", "*"])
        rc, _, err = _sh(
            ["git", "-C", str(src), "fetch", "origin", "--prune"],
            progress=True,
        )
        if rc != 0:
            res.update(state=("FETCH_STALLED" if rc == watchdog.RC_STALLED
                              else "FETCH_FAILED"), detail=err.strip()[:200])
            return res
        # A clone may have been created with --single-branch.  Widen upstream
        # before fetching for the same reason origin is widened above: the
        # live default branch can change, and a cached upstream/HEAD is not
        # proof of what the remote calls HEAD now.
        rc, _, err = _sh([
            "git", "-C", str(src), "remote", "set-branches", "upstream", "*",
        ])
        if rc != 0:
            res.update(
                state="UPSTREAM_DEFAULT_UNMEASURABLE",
                detail=("could not widen the upstream refspec; the live "
                        "default branch cannot be verified"),
            )
            return res
        rc, _, err = _sh(["git", "-C", str(src), "fetch", "upstream", "--prune"],
                         progress=True)
        if rc != 0:
            res.update(state=("FETCH_STALLED" if rc == watchdog.RC_STALLED
                              else "FETCH_FAILED"), detail=err.strip()[:200])
            return res

        # Ask the remote itself every round.  A local upstream/HEAD is only a
        # cached symbolic ref: Trilinos changed its default from master to
        # develop while that cache continued to resolve cleanly, making this
        # entry point report zero commits as the live branch moved ahead.
        rc, out, err = _live_head_with_transport_retry(src)
        symrefs = []
        head_oids = []
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                    symrefs.append(line[len("ref: refs/heads/"):-len("\tHEAD")])
                elif re.fullmatch(r"[0-9a-fA-F]{40}\tHEAD", line):
                    head_oids.append(line.split("\t", 1)[0].lower())
        if rc != 0 or len(symrefs) != 1 or len(head_oids) != 1 or not symrefs[0]:
            diagnostic = " ".join((err or out).split())
            res.update(
                state="UPSTREAM_DEFAULT_UNMEASURABLE",
                detail=("live upstream HEAD did not resolve to exactly one "
                        "branch and one full commit id"
                        + (f" (rc={rc}: {diagnostic[:220]})"
                           if diagnostic else f" (rc={rc})")),
            )
            return res
        up = symrefs[0]
        observed_upstream_tip = head_oids[0]
        upstream_tracking_ref = f"refs/remotes/upstream/{up}"
        urc, uout, uerr = _sh([
            "git", "-C", str(src), "rev-parse", "--verify",
            f"{upstream_tracking_ref}^{{commit}}",
        ])
        tracking_rows = [line.strip().lower() for line in uout.splitlines()
                         if line.strip()]
        tracking_tip = tracking_rows[0] if len(tracking_rows) == 1 else ""
        res["upstream_default"] = {
            "branch": up,
            "remote_tip": observed_upstream_tip,
            "tracking_tip": tracking_tip or None,
        }
        if (urc != 0 or not re.fullmatch(r"[0-9a-f]{40}", tracking_tip)
                or tracking_tip != observed_upstream_tip):
            diagnostic = " ".join((uerr or uout).split())
            res.update(
                state="UPSTREAM_DEFAULT_UNMEASURABLE",
                detail=(f"fresh upstream/{up} does not exactly match live "
                        f"upstream HEAD {observed_upstream_tip[:12]}; nothing "
                        "was measured, merged, or pushed"
                        + (f" (rc={urc}: {diagnostic[:180]})"
                           if diagnostic else f" (rc={urc})")),
            )
            return res

        # A SHALLOW clone cannot be counted and must not be merged. The shared
        # OpenROAD checkout is grafted at one commit, and `rev-list --count` over
        # it reported 42508 commits behind where GitHub's compare says 19. A
        # merge computed against a graft is worse than a wrong number: it has no
        # true merge base. Deepen first; the cost is once per clone.
        rc, sh_out, _ = _sh(["git", "-C", str(src), "rev-parse",
                             "--is-shallow-repository"])
        if sh_out.strip() == "true":
            rc, _, err = _sh(["git", "-C", str(src), "fetch", "--unshallow",
                              "origin"], progress=True)
            if rc != 0:
                _sh(["git", "-C", str(src), "fetch", "--depth=2147483647",
                     "origin"], progress=True)
            _sh(["git", "-C", str(src), "fetch", "upstream", "--prune"],
                progress=True)
            rc, sh_out, _ = _sh(["git", "-C", str(src), "rev-parse",
                                 "--is-shallow-repository"])
            if sh_out.strip() == "true":
                res.update(state="SHALLOW_NEEDS_HUMAN",
                           detail="clone is shallow and could not be deepened; "
                                  "any count or merge over it is meaningless")
                return res

        # Re-prove branch identity AFTER the fetch/deepen and BEFORE creating a
        # candidate worktree. The API lookup and this action are separated by
        # real time; a force-push in that window must stop the merge, not silently
        # retarget it to whatever now happens to wear the declared branch name.
        declared = _declared_branch(repo)
        # Direct callers predating `expected_pin` have no lookup observation to
        # re-prove. Keep their explicitly supplied branch working; production
        # main always supplies the pin for pinned tools and got `branch` from the
        # declaration-aware lookup above.
        identity_branch = (declared or branch) if expected_pin else branch
        identity = {
            "declared_branch": declared,
            "selected_branch": branch,
            "expected_pin": expected_pin or None,
        }
        res["branch_identity"] = identity
        if expected_pin and declared and branch != declared:
            identity.update(outcome="changed", rc=1)
            res.update(
                state="BRANCH_IDENTITY_CHANGED",
                detail=(f"lookup selected {branch!r}, but FORKS.json now declares "
                        f"{declared!r}; nothing was merged or pushed"),
            )
            return res

        # Freeze the freshly-fetched remote-tracking ref to one full object id.
        # Everything below (pin proof, candidate base, and publication lease)
        # consumes this same immutable observation.  Using origin/<branch>
        # again later would reopen a race with another process fetching this
        # shared checkout; a plain final push would additionally allow a remote
        # rewind to be overwritten as an apparently-valid fast-forward.
        origin_tracking_ref = f"refs/remotes/origin/{identity_branch}"
        trc, tout, terr = _sh([
            "git", "-C", str(src), "rev-parse", "--verify",
            f"{origin_tracking_ref}^{{commit}}",
        ])
        tip_rows = [line.strip() for line in tout.splitlines() if line.strip()]
        if (trc != 0 or len(tip_rows) != 1
                or not re.fullmatch(r"[0-9a-fA-F]{40}", tip_rows[0])):
            identity.update(outcome="unmeasurable", tip_rc=trc)
            diagnostic = " ".join((terr or tout).split())
            res.update(
                state="BRANCH_IDENTITY_UNMEASURABLE",
                detail=(f"could not resolve fresh origin/{identity_branch} to "
                        "exactly one full commit id; nothing was merged or pushed"
                        + (f" (rc={trc}: {diagnostic[:220]})"
                           if diagnostic else f" (rc={trc})")),
            )
            return res
        observed_origin_tip = tip_rows[0].lower()
        identity["observed_origin_tip"] = observed_origin_tip

        if expected_pin:
            if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_pin):
                identity.update(outcome="unmeasurable", rc=None)
                res.update(
                    state="BRANCH_IDENTITY_UNMEASURABLE",
                    detail=(f"expected pin {expected_pin!r} is not a full commit "
                            "id; nothing was merged or pushed"),
                )
                return res
            irc, iout, ierr = _sh([
                "git", "-C", str(src), "merge-base", "--is-ancestor",
                expected_pin, observed_origin_tip,
            ])
            identity["rc"] = irc
            if irc == 1:
                identity["outcome"] = "changed"
                res.update(
                    state="BRANCH_IDENTITY_CHANGED",
                    detail=(f"fresh origin/{identity_branch} no longer contains "
                            f"expected pin {expected_pin[:9]}; the branch moved "
                            "between lookup and merge, so nothing was pushed"),
                )
                return res
            if irc != 0:
                identity["outcome"] = "unmeasurable"
                diagnostic = " ".join((ierr or iout).split())
                res.update(
                    state="BRANCH_IDENTITY_UNMEASURABLE",
                    detail=(f"could not prove expected pin {expected_pin[:9]} "
                            f"is still on origin/{identity_branch} (rc={irc})"
                            + (f": {diagnostic[:220]}" if diagnostic else "")),
                )
                return res
            identity["outcome"] = "confirmed"
        else:
            identity.update(outcome="not_applicable_unpinned", rc=0)

        rc, _, err = _sh(["git", "-C", str(src), "worktree", "add", "--detach",
                          str(wt), observed_origin_tip], progress=True)
        if rc != 0:
            res.update(state=("WORKTREE_STALLED" if rc == watchdog.RC_STALLED
                              else "WORKTREE_FAILED"), detail=err.strip()[:200])
            return res

        rc, out, _ = _sh(["git", "rev-list", "--count",
                          f"HEAD..{observed_upstream_tip}"], wt)
        if rc != 0 or not out.strip().isdigit():
            # My first version stored -1 here and printed `WOULD_MERGE -1
            # commit(s)`. That reads as a merge decision when it is a FAILED
            # MEASUREMENT, which is the exact confusion this program exists to
            # stop. A count we could not take is a state of its own.
            res.update(state="COUNT_FAILED",
                       detail=f"could not count HEAD..upstream/{up} — the "
                              f"branch may not exist on upstream")
            return res
        behind = int(out.strip())
        res["took"] = behind
        if behind == 0:
            res.update(state="ALREADY_CURRENT", detail=f"level with upstream/{up}")
            return res
        if dry:
            res.update(state="WOULD_MERGE",
                       detail=f"{behind} commit(s) behind upstream/{up}; "
                              f"{len(checks)} post-merge check(s) declared")
            return res

        rc, _, err = _sh(["git", "-c", "user.name=vibeic-fork-gatekeeper",
                          "-c", "user.email=fork-gatekeeper@vibeic.ai",
                          "merge", observed_upstream_tip, "-m",
                          f"Merge upstream/{up} into {branch} (daily, owner ruling 2026-07-29)"],
                         wt, progress=True)
        if rc != 0:
            if rc == watchdog.RC_STALLED:
                _sh(["git", "merge", "--abort"], wt)
                res.update(
                    state="MERGE_STALLED",
                    detail="merge made no output/process-tree CPU progress; "
                           "no conflict verdict was formed and nothing was pushed",
                )
                return res
            _, cf, _ = _sh(["git", "diff", "--name-only", "--diff-filter=U"], wt)
            res["conflicts"] = [c for c in cf.splitlines() if c.strip()]
            # ABORT, do not guess. Merging everything is the owner's policy;
            # resolving a conflicting hunk is a judgement, and a wrong one
            # compiles.
            _sh(["git", "merge", "--abort"], wt)
            res.update(state="CONFLICT_NEEDS_HUMAN",
                       detail=f"{len(res['conflicts'])} file(s) conflicted; merge aborted, "
                              f"fork left at its previous tip")
            return res

        # Our own commits must still be REACHABLE. A merge that exits 0 having
        # dropped them is the failure this check exists for.
        rc, out, _ = _sh(["git", "rev-list", "--count",
                          f"{observed_upstream_tip}..HEAD"], wt)
        ours = int(out.strip() or 0) if rc == 0 else -1

        # POST-MERGE CHECKS (vibeic-eda#89) — BEFORE the push, deliberately.
        #
        # Running them after would mean the broken merge is already on the build
        # branch and the report is an obituary. Running them here makes a failing
        # check produce exactly the outcome a textual conflict produces: nothing
        # published, fork left at its previous tip, named in the report, and the
        # program exits 1 so the tick that calls it cannot report a clean day.
        res["checks"] = run_post_merge_checks(wt, checks)
        failed = [c for c in res["checks"] if not c["ok"]]
        if failed:
            outcomes = {c.get("outcome") for c in failed}
            if any(c.get("outcome") == "natural" and c.get("rc") != 0
                   for c in failed):
                state = "POST_MERGE_CHECK_FAILED"
            elif "stalled" in outcomes:
                state = "POST_MERGE_CHECK_STALLED"
            elif any(outcome != "natural" for outcome in outcomes):
                state = "POST_MERGE_CHECK_UNMEASURABLE"
            else:
                state = "POST_MERGE_CHECK_FAILED"
            finding = {
                "POST_MERGE_CHECK_FAILED": "returned a complete red result",
                "POST_MERGE_CHECK_STALLED": "made no validated forward progress",
                "POST_MERGE_CHECK_UNMEASURABLE": "produced no complete record",
            }[state]
            res.update(state=state,
                       detail=f"{len(failed)} of {len(res['checks'])} post-merge "
                              f"check(s) {finding}; NOT pushed, "
                              f"fork left at its previous tip — "
                              + "; ".join(f"{c['name']} rc={c['rc']}: {c['detail']}"
                                          for c in failed)[:400])
            return res

        publication = {
            "mode": "explicit-force-with-lease-cas",
            "ref": f"refs/heads/{identity_branch}",
            "expected_old_tip": observed_origin_tip,
            "outcome": "not_attempted",
        }
        res["publication"] = publication

        # --force-with-lease supplies the remote compare-and-swap, but its
        # "force" half must never become permission to publish a candidate
        # which dropped the observed branch.  A post-merge checker is an
        # executable program and could accidentally move HEAD; prove the
        # fast-forward relation again at the last possible local moment.
        arc, aout, aerr = _sh([
            "git", "merge-base", "--is-ancestor", observed_origin_tip, "HEAD",
        ], wt)
        if arc != 0:
            diagnostic = " ".join((aerr or aout).split())
            candidate_changed = arc == 1
            publication.update(
                outcome="not_published",
                classification=("needs_human" if candidate_changed else "unknown"),
                rc=arc,
                diagnostic=diagnostic[:220],
            )
            res.update(
                state=("PUSH_CANDIDATE_NOT_FAST_FORWARD" if candidate_changed
                       else "PUSH_CANDIDATE_UNMEASURABLE"),
                detail=(f"candidate no longer proves a fast-forward from "
                        f"observed origin/{identity_branch} "
                        f"{observed_origin_tip[:12]} (rc={arc}); not pushed"
                        + (f": {diagnostic[:200]}" if diagnostic else "")),
            )
            return res

        rc, push_out, err = _sh(
            ["git", "push", "--porcelain", "--progress",
             f"--force-with-lease=refs/heads/{identity_branch}:"
             f"{observed_origin_tip}",
             "origin", f"HEAD:refs/heads/{identity_branch}"],
            wt, progress=True,
        )
        if rc != 0:
            diagnostic = " ".join(
                part for part in (push_out.strip(), err.strip()) if part
            )
            rejected = any(line.startswith("!\t")
                           for line in push_out.splitlines())
            publication.update(
                outcome=("rejected" if rejected else "unknown"),
                classification=("needs_human" if rejected else "unknown"),
                rc=rc,
                diagnostic=diagnostic[:220],
            )
            if rc == watchdog.RC_STALLED:
                state = "PUSH_CAS_UNCONFIRMED"
            elif rejected:
                state = "PUSH_CAS_REJECTED"
            else:
                state = "PUSH_CAS_UNCONFIRMED"
            res.update(
                state=state,
                detail=(f"CAS publication of refs/heads/{identity_branch} "
                        f"against expected tip {observed_origin_tip[:12]} "
                        f"was {publication['outcome']} (rc={rc}); never "
                        "reported MERGED"
                        + (f": {diagnostic[:200]}" if diagnostic else "")),
            )
            return res
        publication.update(outcome="published", classification="published", rc=0)
        res.update(state="MERGED",
                   detail=f"took {behind} upstream commit(s); {ours} of ours still ahead")
        return res
    finally:
        _sh(["git", "-C", str(src), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(tmp, ignore_errors=True)
        after = _sh(["git", "status", "--porcelain"], src)[1]
        if after != before:
            res["detail"] += ("  !! the shared checkout's working tree CHANGED "
                              "during this merge — investigate")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--only", default=None, help="one fork, for testing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    pins = build_branches(Path(a.eda_root))
    if a.only:
        pins = {k: v for k, v in pins.items() if k == a.only}
    branches, lookup_rows = {}, []
    for repo, pin in sorted(pins.items()):
        lookup = branch_at_pin(repo, pin)
        if lookup.status == BRANCH_FOUND and lookup.branch:
            branches[repo] = lookup.branch
            continue
        state = ("NO_BRANCH_AT_PIN" if lookup.status == BRANCH_ABSENT
                 else "BRANCH_LOOKUP_UNKNOWN")
        lookup_rows.append({
            "repo": repo,
            "branch": "",
            "state": state,
            "detail": lookup.detail,
            "conflicts": [],
            "checks": [],
            "took": 0,
            "branch_lookup": {
                "status": lookup.status,
                "rc": lookup.rc,
                "pin": pin,
            },
        })
    if not branches and not lookup_rows:
        print("[NOT CHECKED] no fork/branch pairs found in the Dockerfiles — "
              "nothing was attempted, which is not success", file=sys.stderr)
        return RC_NOTHING

    # A declaration attached to a tool this run never merges is a gate that
    # cannot fire, and it looks exactly like a gate that passed. FORKS.json's
    # `tool` is a free-text key: one typo and the check is silently off. Say so.
    # (`--only` narrows the run on purpose, so it is not a finding there.)
    declared_for = {e.get("tool") for e in _forks_json().get("forks", [])
                    if e.get("post_merge_check")}
    orphaned = sorted(t for t in declared_for if t and t not in branches)
    if orphaned and not a.only:
        print(f"[warn] FORKS.json declares post_merge_check for "
              f"{', '.join(orphaned)}, which this run does not merge — those "
              f"checks cannot fire, which is not the same as passing",
              file=sys.stderr)

    results = lookup_rows + [
        merge_one(r, b, a.dry_run, expected_pin=pins.get(r) or "")
        for r, b in sorted(branches.items())
    ]
    merged = [r for r in results if r["state"] == "MERGED"]
    human = [r for r in results if r["state"] not in ("MERGED", "ALREADY_CURRENT",
                                                      "WOULD_MERGE")]

    blocked = [r for r in results
               if r["state"].startswith("POST_MERGE_CHECK_")]

    print(f"daily_merge: {len(results)} fork(s), {len(merged)} merged, "
          f"{sum(r['took'] for r in merged)} upstream commit(s) taken, "
          f"{len(human)} need a human"
          + (f", {len(blocked)} blocked by a post-merge check" if blocked else ""))
    for r in results:
        print(f"  {r['repo']:<14} {r['state']:<22} {r['detail'][:80]}")
        for c in r["conflicts"][:10]:
            print(f"      conflict: {c}")
        # Print the checks that RAN as well as the ones that failed. A gate is
        # only believable if its green is visible too: "0 failures" out of zero
        # checks executed is the state this whole issue is about.
        for c in r.get("checks", []):
            print(f"      check {'ok  ' if c['ok'] else 'FAIL'} "
                  f"{c['name']}: {c['detail'][:120]}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "daily_merge", "forks": results}, indent=2) + "\n",
            encoding="utf-8")

    if human:
        print(f"[NEEDS HUMAN] {len(human)} fork(s) were not published; each is "
              f"left at its previous tip", file=sys.stderr)
        if blocked:
            # Named separately because the two failures need different actions.
            # A conflict is "git could not merge this"; a post-merge check is
            # "git merged it but the declared evidence was red or incomplete"
            # — a clean merge that must not ship, with the exact evidence state
            # retained on each check row.
            print(f"[POST-MERGE CHECK] {', '.join(r['repo'] for r in blocked)} "
                  f"merged with NO textual conflict and was blocked by a declared "
                  f"check on the merged tree (vibeic-eda#89)", file=sys.stderr)
        return RC_NEEDS_HUMAN
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
