#!/usr/bin/env python3
"""The 05:30 procedure. One line per fork, every day, end to end.

OWNER RULING 2026-07-31
=======================
    "我們自己所有分支裡面的 commit，也都要進到我們的 master 裡面。
     進去以後，確認 image 都 build 過了。把這些分支清一下。"
    "分支是為了要驗證某一個 PR 有沒有結束，結束了以後，確認這些 commit
     就把它進到這個 main 裡面，然後這個 main 就繼續走。"

A fork has ONE line. Branches are scaffolding for a PR and are removed when the
PR is done. Upstream merges into that line, our work merges into that line, the
image pins that line.

WHY THIS PROGRAM EXISTS (measured 2026-07-31)
---------------------------------------------
Two mechanisms already ran daily and both were individually correct:
  * `daily_merge.py` brought upstream INTO the forks — it never asked which of
    our branches the image builds from.
  * `daily_release.py` moved each pin to "its fork's tip" — and the shipped
    branch's tip genuinely had not moved.
Between them sat a question nobody asked: **is there work of ours the shipped
line cannot reach?** There was. 14 forks held work on branches the image did not
build, klayout carried 125 branches, ngspice 182, yosys 156.

THE MEASUREMENT THAT MATTERS, and the one I got wrong first
-----------------------------------------------------------
Counting by SHA reachability (`git rev-list main..branch`) gave 273 stranded
commits. Counting by PATCH EQUIVALENCE (`git cherry`) gave **2**. The difference
is not rounding: parallel feature branches each carried a COPY of the same fix,
so the same work was counted up to fifteen times. `git cherry` is used
throughout here, because "is this work on master" is a question about CONTENT,
not about which SHA can reach which.

ORDER IS LOAD-BEARING
---------------------
upstream -> master  BEFORE  branches -> master. A branch merged onto a stale
master then has to be re-merged after the upstream merge moves it, and the
second merge is the one that conflicts.

WHAT IT REFUSES
---------------
* It never deletes a branch `git cherry` says still holds unique work.
* The ordinary merge path never force-pushes. The sole exception is an explicit
  machine-readable watch-only policy: it first preserves the exact displaced
  tip under a deterministic remote tag, then resets with an exact-SHA lease.
  A rejected lease is reported, never overridden — on 2026-07-31 a `master`
  that looked stale locally was 7924 upstream commits BEHIND its own origin,
  and an unguarded force would have destroyed them.
* It never cuts an image version when no tool changed.
* It never merges a branch whose pull request is still OPEN on the fork, or
  whose newest pull request was closed unmerged, and it refuses (needs_human)
  when GitHub cannot say which: the PR is the gatekeeper's decision point, and
  consolidation that merges past it took the decision away (librelane #16/#17,
  2026-09-27). See `consolidation_verdict`.
* It never RESOLVES a conflict by itself. A merge conflict between our own work
  and upstream is a judgement call, and pretending otherwise is how a fix gets
  silently dropped.

THE AI HALF (step 2b, owner ruling 2026-07-31)
----------------------------------------------
    "Merge 或者是 Cherry-pick 的話，是需要用到 AI 的，用程式直接判斷是不夠的。"

Refusing to resolve a conflict is right. STOPPING there was not: the earlier
version flagged `needs_human` and moved on, so a conflicted branch simply sat,
and the six steps completed while the work they exist to consolidate did not.

So the script does the mechanical half and hands the judgement half to this
host's gatekeeper AI, with the evidence attached: the conflicting files, our
commits on that branch with their subjects and touched files, and git's own
message. The gatekeeper decides MERGE vs CHERRY-PICK per case, resolves on the
merits, and lands it. `--no-ai` writes the brief without invoking, which is what
a dry run does. A launched turn is not itself a completed decision: the case
clears only after its captured source tip is proved present, by ancestry or
patch equivalence, on the fork's published mainline.

The invocation BLOCKS under a forward-progress lease. Detaching it would end
this process before the gatekeeper's turn produced anything, which is the exact
failure that has already cost this campaign four rounds on one cell. Elapsed
runtime is not a verdict: a progressing turn may run indefinitely; only a turn
whose stream/tool/CPU signals all stay flat for the stall grace is stopped.

Exit: 0 clean, 1 something needs a human, 2 nothing could be checked.
"""
from __future__ import annotations
import argparse, json, os, re, secrets, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gk_state
import progress_watchdog as watchdog
import round_record
import watch_only_reset
# ONE mechanism, both mergers (vibeic-eda#89). This module and `daily_merge.py`
# are two automatic paths from upstream onto a published branch, and this is the
# one the 05:30 wrapper runs FIRST. Wiring the post-merge checks into only the
# other one would have produced a gate that is correct, tested, and absent from
# the path that actually merges every morning — the defect the issue is about.
from daily_merge import post_merge_checks, run_post_merge_checks
from daily_merge import _declared_branch as declared_branch

# Overridable so the AI-handoff path can be exercised against a synthetic fork.
# The step it guards only fires on a real merge conflict, and a conflict that
# never happens on a given morning is not evidence the handoff works.
FORKS = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")

# Step 1 defers branch consolidation when the upstream merge did not land on its
# own. The exact text is a CONSTANT because two places must agree on it: the step
# that defers, and `resume_ours_after_ai_landing` below, which is allowed to
# resume only THIS deferral -- never the `BRANCH LIST UNKNOWN` one, which means
# the branch list was never read and no later evidence can supply it.
OURS_DEFERRED_BY_UPSTREAM_CONFLICT = (
    "upstream landing did not form a verified successful "
    "record; branch consolidation waits for the required "
    "upstream-first base")
EDA = Path("/home/reyerchu/vibeic-eda")
OURS = ("reyer", "vibeic")
# Destructive fork cleanup is scoped to the organisation this gate owns.  A
# valid-looking GitHub URL for some other owner is not evidence about our PRs.
FORK_OWNER = "vibeic"

# THE FLEET IS THE REGISTRY, NOT THE DIRECTORY LISTING.  The round used to walk
# whatever clones happened to sit under FORKS, while the release authorizer
# demands one row per FORKS.json entry.  cace, eqy and spicebind were registered
# on 2026-09-26 (vibeic-eda #243/#244/#245) and nothing ever cloned them, so the
# 2026-09-27 05:30 round walked 39 of 42 forks, exited on its conflicts, and the
# authorizer found the gap 15 minutes later as "missing top-level row(s)".  A
# registered fork with no checkout is now cloned at the START of the round, and
# one that cannot be cloned gets an error row naming it -- never no row.
FORKS_JSON = Path(__file__).resolve().with_name("FORKS.json")
#: Where a missing registered fork is cloned from / its upstream lives.  Module
#: attributes (not literals inside the function) so a test can point them at
#: local bare repositories instead of GitHub.
FORK_CLONE_URL = "https://github.com/" + FORK_OWNER + "/{tool}.git"
UPSTREAM_CLONE_URL = "https://github.com/{slug}.git"


def registered_fleet(path=None):
    """Return ``(entries, errors)`` for every fork FORKS.json registers.

    ``entries`` maps tool -> its registry row.  Any error means the fleet is
    unknown and the round must not pretend it walked all of it.
    """
    path = Path(path or FORKS_JSON)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return {}, [f"could not read the registered fleet {path}: "
                    f"{type(exc).__name__}: {exc}"]
    rows = raw.get("forks") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}, [f"registered fleet {path} has no forks list"]
    entries, errors = {}, []
    for pos, row in enumerate(rows):
        tool = row.get("tool") if isinstance(row, dict) else None
        if (not isinstance(tool, str) or not tool or tool != tool.strip()
                or "/" in tool or tool.startswith(".")
                or any(ch.isspace() for ch in tool)):
            errors.append(f"registered fleet forks[{pos}].tool is malformed")
            continue
        if tool in entries:
            errors.append(f"registered fleet has duplicate tool {tool}")
            continue
        entries[tool] = row
    return entries, errors


def provision_registered_checkouts(fleet, apply):
    """Clone every registered fork that has no checkout under FORKS.

    Returns ``{tool: reason}`` for each registered fork that still has no
    usable checkout afterwards.  The caller turns each into an error row, so
    an unclonable fork fails the round naming itself instead of vanishing from
    the record.  Nothing that already exists at a fork's path is touched.
    """
    missing = {}
    for tool, entry in sorted(fleet.items()):
        d = FORKS / tool
        if (d / ".git").is_dir():
            continue
        if d.exists() or d.is_symlink():
            missing[tool] = (f"{d} exists but is not a git clone; left "
                             "untouched")
            continue
        if not apply:
            missing[tool] = (f"no local checkout at {d}; a dry run does not "
                             "clone")
            continue
        slug = entry.get("upstream")
        if not isinstance(slug, str) or slug.count("/") != 1 \
                or any(ch.isspace() for ch in slug):
            missing[tool] = ("no local checkout, and FORKS.json declares no "
                             "usable upstream (owner/repo) to configure")
            continue
        print(f"  {tool:<20} registered in FORKS.json with no local checkout "
              f"— cloning into {d}")
        FORKS.mkdir(parents=True, exist_ok=True)
        cp = sh("git", "clone", "--progress",
                FORK_CLONE_URL.format(tool=tool), str(d), progress=True)
        if cp.returncode != 0:
            missing[tool] = (
                ("clone STALLED" if cp.returncode == watchdog.RC_STALLED
                 else f"clone failed (rc={cp.returncode})")
                + f": {_first_error_line(cp)}")
            continue
        add = sh("git", "-C", str(d), "remote", "add", "upstream",
                 UPSTREAM_CLONE_URL.format(slug=slug))
        if add.returncode != 0:
            missing[tool] = ("cloned, but the declared upstream remote could "
                             f"not be added: {_first_error_line(add)}")
    return missing


#: `sh` never got an answer to a deliberately short request/probe, or could not
#: be launched. DISTINCT from any exit code the tool itself chose, so a caller
#: can tell "we never got an answer" apart from "the tool answered non-zero".
RC_NO_ANSWER = 124
#: A single-ref `ls-remote` is one request and no object transfer.
LS_REMOTE_TIMEOUT_S = 120
#: Fetch, merge, checkout and push are work, not request probes.  Their elapsed
#: runtime is unbounded; only a period with no output and no process-tree CPU
#: progress expires the lease.  The grace is deliberately configurable without
#: becoming a claim about how long a healthy operation "should" take.
GIT_STALL_GRACE_S = float(os.environ.get("GK_GIT_STALL_GRACE", "300"))
GIT_PROGRESS_POLL_S = float(os.environ.get("GK_GIT_PROGRESS_POLL", "1"))
# GitHub REST calls below are request/response probes, not long-running work.
# This deadline can only produce NORECORD; it is never translated into "no PR"
# or any merge/prune verdict.
GITHUB_API_TIMEOUT_S = float(os.environ.get("GK_GITHUB_API_TIMEOUT", "180"))


def sh(*a, cwd=None, timeout=None, progress=False,
       stall_grace=GIT_STALL_GRACE_S):
    """Run a command and return its CompletedProcess.

    Long work opts into a renewable progress lease.  It may run indefinitely
    while output bytes or process-tree CPU advance; a flat lease returns the
    distinct ``RC_STALLED`` state and is never rewritten as a git failure or a
    merge conflict.  A wall-clock timeout is available only to explicitly short
    request/probe calls such as ``ls-remote``.  Local metadata reads have no
    elapsed-time verdict at all.
    """
    try:
        if progress:
            run = watchdog.run_supervised(
                list(a), cwd=cwd, stall_grace_s=stall_grace,
                poll_s=GIT_PROGRESS_POLL_S,
                output_progress=True, process_tree_cpu_progress=True,
            )
            cp = subprocess.CompletedProcess(a, run.rc, run.out, run.err)
            cp.supervision_outcome = run.outcome
            cp.elapsed_seconds = run.elapsed_s
            return cp
        kwargs = {"capture_output": True, "text": True, "cwd": cwd}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return subprocess.run(a, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            a, RC_NO_ANSWER, "",
            f"no answer: timed out after {timeout}s")
    except OSError as exc:                                  # noqa: BLE001
        return subprocess.CompletedProcess(
            a, RC_NO_ANSWER, "", f"no answer: {type(exc).__name__}: {exc}")


def out(*a, cwd=None):
    return sh(*a, cwd=cwd).stdout.strip()


CAUSE_LINE_CAP = 160          # how much of a cause line is reported
_ELISION = " ... "            # marks where the middle was cut


def _first_error_line(cp) -> str:
    """The line of a failed command's output that NAMES the cause.

    git puts the diagnosis FIRST and the advice last. On an unreachable remote
    it emits, in order::

        fatal: '/path/gone' does not appear to be a git repository
        fatal: Could not read from remote repository.
        (blank)
        Please make sure you have the correct access rights
        and the repository exists.

    so the LAST line is `and the repository exists.` — a sentence fragment that
    identifies nothing. Reporting it publishes the least informative line git
    wrote. Take the first non-empty one.

    A line too long to report whole loses its MIDDLE, never its tail. git
    writes the subject first and the diagnosis last on that same line, so a
    tail cut keeps the path -- which the reader already knows -- and drops the
    only words this function exists to surface. Under a deep enough checkout
    path the path IS the whole cap, and the function silently returns nothing
    that names anything.
    """
    lines = ((cp.stderr or "") + "\n" + (cp.stdout or "")).splitlines()
    # --progress can precede the actual diagnosis with successful transfer
    # messages. Preserve the first error, then fall back to arbitrary output.
    diagnoses = [ln for ln in lines
                 if re.match(r"\s*(?:remote:\s*)?(?:fatal|error):", ln)]
    for ln in diagnoses or lines:
        ln = ln.strip()
        if ln:
            if len(ln) <= CAUSE_LINE_CAP:
                return ln
            head = (CAUSE_LINE_CAP - len(_ELISION)) // 2
            tail = CAUSE_LINE_CAP - len(_ELISION) - head
            return f"{ln[:head]}{_ELISION}{ln[-tail:]}"
    return "no error text"


def mainline(g):
    for c in ("main", "master"):
        if out(*g, "rev-parse", "--verify", "-q", f"refs/heads/{c}"):
            return c
    return None


def published_mainline(g, repo):
    """Return the declared line this fork actually publishes.

    ``main``/``master`` is only a naming convention.  The release pipeline
    already treats ``FORKS.json`` as authoritative for branch identity, so the
    earlier 05:30 merge must use the same declaration.  Otherwise a maintained
    image branch can stay frozen while this program successfully merges and
    pushes a different conventional branch (measured on Trilinos, 2026-08-25).

    A declared branch must already be visible locally or under ``origin``.  The
    production caller fetches origin immediately before asking; absence is
    therefore NORECORD, not permission to guess main/master.
    """
    branch = declared_branch(repo)
    if branch:
        for ref in (f"refs/heads/{branch}",
                    f"refs/remotes/origin/{branch}"):
            if out(*g, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"):
                return branch
        return None
    # Compatibility for synthetic/legacy registries. Production declares all
    # 37 rows, while tests and old callers historically supplied only a clone.
    return mainline(g)


def checkout_published_line(g, branch):
    """Check out the freshly fetched published branch without discarding work.

    A shared clone can have a stale local branch.  Fast-forward it when safe;
    retain and refuse when it diverged.  Returning structured evidence keeps a
    failed branch selection from being flattened into a generic checkout error.
    """
    remote = f"origin/{branch}"
    remote_tip = out(*g, "rev-parse", "--verify", "-q",
                     f"{remote}^{{commit}}")
    evidence = {
        "declared_branch": branch,
        "remote_ref": remote,
        "remote_tip": remote_tip or None,
        "local_tip_before": out(*g, "rev-parse", "--verify", "-q",
                                f"refs/heads/{branch}^{{commit}}") or None,
        "outcome": "norecord",
    }
    if not remote_tip:
        evidence["reason"] = f"freshly fetched {remote} is absent or unreadable"
        return False, evidence

    if evidence["local_tip_before"]:
        checkout = sh(*g, "checkout", branch, progress=True)
    else:
        checkout = sh(*g, "checkout", "--track", "-b", branch, remote,
                      progress=True)
    evidence["checkout_rc"] = checkout.returncode
    if checkout.returncode != 0:
        evidence["outcome"] = (
            "stalled" if checkout.returncode == watchdog.RC_STALLED
            else "natural_failure")
        evidence["reason"] = _first_error_line(checkout)
        return False, evidence

    # This is deliberately a merge, not reset/update-ref: an ahead local line
    # remains intact, a behind one advances, and a divergent one is refused.
    ff = sh(*g, "merge", "--ff-only", remote, progress=True)
    evidence["fast_forward_rc"] = ff.returncode
    if ff.returncode != 0:
        evidence["outcome"] = (
            "stalled" if ff.returncode == watchdog.RC_STALLED
            else "diverged")
        evidence["reason"] = _first_error_line(ff)
        evidence["local_tip_after"] = out(*g, "rev-parse", "HEAD") or None
        return False, evidence

    evidence["local_tip_after"] = out(*g, "rev-parse", "HEAD") or None
    evidence["outcome"] = "complete"
    return True, evidence


def image_pins(eda_root=None, *, fail_closed=False):
    """`{lowercased tool name: pinned sha}` for every fork an image Dockerfile
    pins, or `{}` when they could not be read.  Destructive callers pass
    ``fail_closed=True`` and receive ``None`` for that latter state instead.

    Retained per round (vibeic-eda#85) because "what did the round decide" is
    only half a record — WHICH COMMIT the shipping image was pinned at that
    morning is what makes a past row actionable, and it is the one field that
    cannot be recovered later from the fork clone alone.

    Read through `discover_forks.parse_dockerfile_pins`, which is the module
    that already owns this grammar; a second parser here is how two programs
    come to disagree about the same four pins. Import failure is not fatal —
    the pin field goes null and the round continues, because retention must
    never be the reason a morning does not run.

    `eda_root` defaults to `None`, not to `EDA`: a default bound at def time
    freezes the module global as it was AT IMPORT, so every other reader of
    `EDA` in this file could be redirected and this one alone could not. That
    asymmetry is invisible until something depends on it.
    """
    try:
        import discover_forks                                # noqa: PLC0415
        eda_root = Path(eda_root if eda_root is not None else EDA)
        if fail_closed and not eda_root.is_dir():
            raise FileNotFoundError(f"EDA root is not readable: {eda_root}")
        files = list((Path(eda_root) / "tools").glob("*/Dockerfile"))
        if (Path(eda_root) / "Dockerfile").is_file():
            files.append(Path(eda_root) / "Dockerfile")
        if fail_closed and not files:
            raise FileNotFoundError(f"no Dockerfiles below {eda_root}")
        pins = {}
        for f in files:
            for tool, rec in discover_forks.parse_dockerfile_pins(
                    f.read_text(errors="replace")).items():
                ref = rec.get("ref") if isinstance(rec, dict) else rec
                if ref:
                    pins[str(tool).lower()] = ref
        return pins
    except Exception:                                       # noqa: BLE001
        return None if fail_closed else {}


# ── A COMPARISON IS ONLY AS GOOD AS THE REF IT IS MADE AGAINST ───────────────
#
# Every question this round asks about a fork — "are we behind upstream", "which
# branches does the fork have" — is answered by reading a REMOTE-TRACKING REF
# and comparing. Those refs are only meaningful if the fetch that was supposed
# to refresh them actually did. Both fetches in this file discarded the
# `CompletedProcess` entirely, so a fetch that failed left the comparison to run
# against whatever the clone last managed to fetch, and a stale ref reports
# `behind == 0` — "we could not reach upstream" rendered as "we are up to date".
#
# WHY THIS GUARDS THE CLASS AND NOT ONE STORY. The obvious fix is to read the
# exit status, and that is necessary but NOT sufficient: it only covers failures
# git chooses to report as failures. It was proposed on the strength of a clone
# whose auto-gc had failed (`gc.log`: "There are too many unreachable loose
# objects", 6935 of them), on the theory that a fetch in that state does not
# update the ref. That theory is UNSUBSTANTIATED. Re-run against a copy of that
# exact clone — same 6935 unreachable loose objects, same gc.log, the tracking
# ref rewound to the value it held that morning — `git fetch upstream --quiet`
# returned **0** and moved the ref correctly, emitting the gc complaint as a
# warning on stderr. A declined auto-gc warns; it does not fail the fetch and it
# does not suppress the ref update. (Not "refuted": the original clone's state
# could not be reproduced exactly, only its object count and its gc.log.)
#
# So an rc-only guard would have been written FROM a case it does not cover, and
# would report success for every fetch that exits 0 without refreshing anything.
# What the comparison actually needs is not "the fetch reported success" but
# "the ref is confirmed to be the remote's current tip", and only the REMOTE can
# confirm that. Hence: read the status, and when the fetch moved nothing — the
# one case where success and silent no-op look identical — ask the remote.
#
# COST, stated because it is a network call per fork per round: one single-ref
# `ls-remote` on the ~35 forks that are already current, ~0.3-1 s each against a
# round that runs for minutes. It is skipped entirely whenever the fetch moved
# the ref, because a ref that moved is proof the fetch reached the remote.


def _fetch(g, remote, *extra):
    """`git fetch`, with its exit status READ. Returns `(ok, why)`.

    `ok=False` means the tracking state for `remote` is UNKNOWN — which is
    neither "current" nor "behind", and must not be rendered as either.
    """
    # ``--progress`` makes object transfer visible even under cron redirection.
    # It is evidence for the renewable lease, not verdict evidence: a stall is
    # UNKNOWN and never means the remote is current or the fetch failed.
    # This operation measures parent refs. An unrelated upstream feature
    # branch can name an unpublished submodule object unavailable from the
    # fork's configured dependency remote. Recursive fetch then returns 1 even
    # though the parent refs arrived. Dependency checkout/build validation has
    # its own boundary; do not fetch every feature branch's dependencies here.
    fr = sh(*g, "fetch", "--no-recurse-submodules", remote, *extra,
            "--progress", progress=True)
    if fr.returncode == 0:
        return True, ""
    if fr.returncode == watchdog.RC_STALLED:
        what = "FETCH STALLED with no forward progress from"
    elif fr.returncode == RC_NO_ANSWER:
        what = "no answer from"
    else:
        what = f"FETCH FAILED (rc={fr.returncode}) —"
    return False, (f"{what} {remote}: state is UNKNOWN, not current: "
                   f"{_first_error_line(fr)}")


def _remote_confirms(g, remote, branch, local_sha):
    """Does `remote` itself report `branch` at `local_sha`?

    Returns `(ok, why, remote_sha)`. `remote_sha` is None when the remote could
    not be asked, and is RETURNED rather than only compared because it is the
    round's own independent observation of upstream's tip. Answering "was this
    fork really current that morning?" from the retained record needs that
    number in the record; without it the record can only repeat the round's
    conclusion, which is the thing being checked (vibeic-eda#85).

    Asked ONLY when the fetch exited 0 and moved nothing — the single case in
    which "the remote had nothing new" and "the fetch did not refresh the ref"
    produce identical observable state locally. Nothing local can separate them,
    so the question goes to the only party that can answer it.
    """
    r = sh(*g, "ls-remote", remote, f"refs/heads/{branch}",
           timeout=LS_REMOTE_TIMEOUT_S)
    if r.returncode != 0 or not r.stdout.split():
        return False, (
            f"UNVERIFIED — the fetch of {remote} exited 0 and moved nothing, and "
            f"{remote} could not then be asked to confirm {branch} is current "
            f"(ls-remote rc={r.returncode}: {_first_error_line(r)}). "
            f"UNKNOWN, not current."), None
    remote_sha = r.stdout.split()[0]
    if remote_sha != local_sha:
        return False, (
            f"REF NOT CONFIRMED CURRENT — the fetch of {remote} exited 0 and "
            f"moved nothing, but {remote} reports {branch} at {remote_sha[:12]} "
            f"while this clone holds {local_sha[:12]}. Either the fetch did not "
            f"refresh the ref or the remote moved between the two calls; both "
            f"mean a comparison made here is not against {remote}'s current "
            f"tip. UNKNOWN, not current."), remote_sha
    return True, "", remote_sha


def step1_upstream(g, main, rep):
    """Upstream -> master. FIRST, so branch merges land on the final base.

    Writes `rep["upstream_evidence"]` alongside `rep["upstream"]`. The prose
    verdict says what the round CONCLUDED; the evidence block says what it
    OBSERVED, so a later reader can check the one against the other instead of
    taking it. That distinction is the whole of vibeic-eda#85: the retained
    record of a round that said `already current` has to carry enough for
    someone to tell a fork that WAS current from a fork the round could not
    measure, and a verdict string alone never could.
    """
    ev = {"ref": None, "tip_before_fetch": None, "tip_seen": None,
          "fetch": None, "confirmed_by": None, "remote_tip": None,
          "behind": None}
    rep["upstream_evidence"] = ev
    if not out(*g, "remote", "get-url", "upstream"):
        rep["upstream"] = "no upstream remote"
        ev["fetch"] = "no_upstream_remote"
        rep["upstream_merge_outcome"] = "not_applicable"
        return
    # Snapshot EVERY upstream tracking ref before the fetch.  The remote's
    # default branch is resolved below from its live HEAD symref, so limiting
    # this to the conventional main/master pair would make a default-branch
    # rename (Trilinos master -> develop, 2026-08-27) invisible.  The old code
    # fetched develop successfully and then selected the still-resolving master
    # ref, certifying 0 while the real default was 69 commits ahead.
    pre = {}
    pre_refs = sh(*g, "for-each-ref",
                  "--format=%(refname:short)%09%(objectname)",
                  "refs/remotes/upstream")
    if pre_refs.returncode == 0:
        for line in (pre_refs.stdout or "").splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and _FULL_OBJECT_ID.fullmatch(fields[1]):
                pre[fields[0]] = fields[1]

    # A clone made with --single-branch may still have a one-branch upstream
    # refspec.  Widen it before fetching, otherwise discovering that HEAD moved
    # to a new branch only tells us about an object the fetch never retained.
    widen = sh(*g, "remote", "set-branches", "upstream", "*")
    if widen.returncode != 0:
        rep["upstream"] = (
            "UNVERIFIED — could not configure upstream to fetch its current "
            f"default branch (rc={widen.returncode}: "
            f"{_first_error_line(widen)}). UNKNOWN, not current.")
        ev["fetch"] = "failed"
        rep["upstream_merge_outcome"] = "unmeasurable"
        rep["needs_human"] = True
        return
    ok, why = _fetch(g, "upstream")
    if not ok:
        rep["upstream"] = why
        ev["fetch"] = ("stalled" if "STALLED" in why else
                       "no_answer" if "no answer" in why else "failed")
        rep["upstream_merge_outcome"] = "unmeasurable"
        rep["needs_human"] = True
        return
    ev["fetch"] = "ok"
    # ASK THE REMOTE WHICH BRANCH IS DEFAULT on every round.  A cached
    # refs/remotes/upstream/HEAD is configuration, not evidence: ordinary fetch
    # does not update it when GitHub changes the default branch.  Parse both the
    # symref and its immutable tip, then require the freshly fetched tracking
    # ref to equal that same tip.  This also closes the fetch-vs-ls-remote race
    # in the conservative direction: a mismatch is UNKNOWN, never a smaller
    # reassuring count.
    remote_head = sh(*g, "ls-remote", "--symref", "upstream", "HEAD",
                     timeout=LS_REMOTE_TIMEOUT_S)
    symbolic_targets = []
    remote_tips = []
    for line in (remote_head.stdout or "").splitlines():
        fields = line.strip().split()
        if len(fields) == 3 and fields[0] == "ref:" and fields[2] == "HEAD":
            symbolic_targets.append(fields[1])
        elif (len(fields) == 2 and fields[1] == "HEAD"
              and _FULL_OBJECT_ID.fullmatch(fields[0].lower())):
            remote_tips.append(fields[0].lower())
    if (remote_head.returncode != 0 or len(symbolic_targets) != 1
            or len(remote_tips) != 1
            or not symbolic_targets[0].startswith("refs/heads/")):
        rep["upstream"] = (
            "UNVERIFIED — the upstream fetch succeeded, but the remote's "
            "current default branch could not be resolved unambiguously "
            f"(ls-remote rc={remote_head.returncode}: "
            f"{_first_error_line(remote_head)}). UNKNOWN, not current.")
        rep["upstream_merge_outcome"] = "unmeasurable"
        rep["needs_human"] = True
        return
    branch = symbolic_targets[0].removeprefix("refs/heads/")
    ref = f"upstream/{branch}"
    ub = out(*g, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    ev["ref"] = ref
    ev["tip_before_fetch"] = pre.get(ref) or None
    ev["tip_seen"] = ub or None
    ev["remote_tip"] = remote_tips[0]
    if not ub or ub != remote_tips[0]:
        rep["upstream"] = (
            "REF NOT CONFIRMED CURRENT — the fetch succeeded, but "
            f"{ref} is {(ub or 'absent')[:12]} while the remote reports "
            f"its default branch at {remote_tips[0][:12]}. Either the refspec "
            "did not retain that branch or upstream moved during the check; "
            "both mean UNKNOWN, not current.")
        rep["upstream_merge_outcome"] = "unmeasurable"
        rep["needs_human"] = True
        return
    if ub != pre.get(ref):
        # The ref MOVED. That is proof the fetch reached the remote, and it is
        # recorded as such — the confirmation is as strong as the ls-remote one
        # below, and a record that left it blank here would make every ordinary
        # merge morning look unverified.
        ev["confirmed_by"] = "fetch_moved_ref"
    else:
        # The live HEAD observation above confirmed the unchanged ref.
        ev["confirmed_by"] = "ls_remote"
    behind = out(*g, "rev-list", "--count", f"{main}..{ub}")
    ev["behind"] = int(behind) if behind.isdigit() else None
    if behind == "0":
        rep["upstream"] = "already current"
        rep["upstream_merge_outcome"] = "natural_success"
        return
    r = sh(*g, "merge", "--no-edit", "-m",
           f"Merge upstream into {main} (daily 05:30)", ub,
           progress=True)
    if r.returncode:
        # Collect the evidence BEFORE aborting, and route it to the SAME AI step
        # that already handles our-branch conflicts. It did not reach that step
        # before: `step2b_ai_decisions` only read `rep["conflicted"]`, which
        # `step2_ours` fills, so an UPSTREAM conflict printed "needs a human" and
        # nothing came back for it.
        #
        # That is backwards for the invariant the owner states first: "upstream's
        # contributions are all in". A conflict taking upstream commits is
        # precisely the case where that invariant is at risk, and it was the one
        # case with no decision-maker. Measured 2026-08-01: OpenSTA (11 upstream
        # commits) and OpenROAD (4) both sat here.
        files = [l for l in out(*g, "diff", "--name-only",
                                "--diff-filter=U").splitlines() if l.strip()]
        if r.returncode == watchdog.RC_STALLED:
            sh(*g, "merge", "--abort")
            rep["upstream"] = (
                f"MERGE STALLED taking {behind} upstream commit(s) — no "
                "forward progress; outcome UNKNOWN, not a conflict")
            rep["upstream_merge_outcome"] = "stalled"
            rep["needs_human"] = True
            return
        if not files:
            sh(*g, "merge", "--abort")
            rep["upstream"] = (
                f"MERGE FAILED taking {behind} upstream commit(s) "
                f"(rc={r.returncode}): {_first_error_line(r)}")
            rep["upstream_merge_outcome"] = "natural_failure"
            rep["needs_human"] = True
            return
        commits = [{"sha": s[:12],
                    "subject": out(*g, "log", "-1", "--format=%s", s),
                    "author": out(*g, "log", "-1", "--format=%an", s)}
                   for s in out(*g, "rev-list", f"{main}..{ub}").split()[:20]]
        sh(*g, "merge", "--abort")
        rep["upstream"] = f"CONFLICT taking {behind} upstream commit(s)"
        rep["upstream_merge_outcome"] = "conflict"
        rep["needs_human"] = True
        rep.setdefault("conflicted", []).append({
            "branch": ub,
            "direction": "upstream -> our mainline",
            "commits": int(behind or 0),
            "conflicting_files": files,
            # NOT `our_commits`: in this direction they are UPSTREAM's, and
            # the brief prints this list under a heading taken from the field.
            # A name that contradicts its contents is how a decision gets made
            # on a false description.
            "commits_detail": commits,
            "merge_stderr": (r.stderr or r.stdout or "").strip()[-800:],
        })
    else:
        rep["upstream"] = f"merged {behind} upstream commit(s)"
        rep["upstream_merge_outcome"] = "natural_success"


def _fork_branches(g, main):
    """Every branch the FORK has, not every branch this clone happens to hold.

    This used to read `refs/heads` — the LOCAL branches. A clone that has only
    ever checked out master has exactly one local branch, so the step reported
    "nothing to consolidate" for a fork whose work sat on branches it had never
    fetched a local ref for.

    Measured 2026-07-31: OpenSTA held 7 of our STA commits (path-based analysis,
    crosstalk delta-delay, glitch screening, vectorless statistical activity) on
    `vibeic/daily-merge-2026-07-29` and `vibeic/sta-timing-eco`, and yosys held
    one on `fix/stat-always-print-cells-row`. Locally OpenSTA had ONE branch:
    master. The step walked past all eight commits and called the fork clean.

    "Which branches does this fork have" is a question about the fork, so it is
    asked of `refs/remotes/origin` after a fetch, not of whatever refs a clone
    was left holding.

    RETURNS None WHEN THE FETCH DID NOT SUCCEED — the same shape `step1_upstream`
    now uses, and for the same reason. This fetch's exit status was discarded
    too, 71 lines below the one that was reported, in the function whose own
    docstring above says the question is asked "after a fetch". If that fetch
    fails the walk runs over whatever `refs/remotes/origin` the clone was left
    holding — exactly the state this function was written to stop reading — and
    a fork whose branches could not be listed comes back as a fork with no
    branches. Every caller then reads that as "nothing to consolidate", which is
    the identical failure-rendered-as-reassurance, one question over.

    An empty LIST still means "this fork has no branches beyond its mainline".
    None means "we could not ask". The callers must not collapse them.
    """
    ok, _why = _fetch(g, "origin", "--prune")
    if not ok:
        return None
    # Exclude BOTH conventional mainline names, not just the one this clone
    # happens to have checked out. A dry run caught `asap7sc7p5t_28` offering
    # `origin/master` for deletion because the local mainline is `main` while the
    # fork's default branch is `master`; the comparison was against the local
    # name and the remote default did not match it.
    # ``main`` may itself contain slashes (Trilinos publishes from
    # ``vibeic/xyce-trilinos-17.2-epetra-restored``).  Keeping only its final
    # path component does not match the remote-tracking spelling and offers the
    # published line back as a topic/prune candidate.
    protected_names = {"main", "master",
                       main.removeprefix("origin/")}
    refs = sh(*g, "for-each-ref", "--format=%(refname:short)",
              "refs/remotes/origin")
    if refs.returncode != 0:
        return None
    out_b = []
    for b in (refs.stdout or "").splitlines():
        b = b.strip()
        # A bare `origin` is the remote ref itself, not a branch. The same dry
        # run offered it for deletion on eight forks.
        if not b or b == "origin" or b.endswith("/HEAD"):
            continue
        if not b.startswith("origin/"):
            continue
        if b.split("/", 1)[1] in protected_names:
            continue
        out_b.append(b)
    return out_b


def _branches_an_image_pin_depends_on(g, eda_root=None):
    """Branches that contain this fork's own image pin, if it has one.

    Deleting one of these is how a pin becomes an orphan: the commit survives
    only as long as a branch reaches it, and afterwards the image builds from
    something git may garbage-collect. That is not hypothetical — `cocotb`'s pin
    `15f2d1017` is reachable from ZERO branches today for exactly this reason.

    A dry run offered `origin/vibeic/batch-honesty-integration` for deletion,
    which is the branch `NGSPICE_REF` points into. Pruning it would have
    manufactured a second cocotb.
    """
    try:
        repo_path = Path(g[g.index("-C") + 1])
        repo_name = repo_path.name.lower()
    except (ValueError, IndexError, TypeError):
        return None
    pins = image_pins(
        eda_root if eda_root is not None else EDA, fail_closed=True)
    if pins is None:
        return None
    pin = pins.get(repo_name)
    if not pin:
        return set()
    keep = set()
    brs = _fork_branches(g, mainline(g) or "master")
    if brs is None:
        return None          # could not list the branches -> cannot tell which
                             # ones a pin needs -> caller must not delete any
    for b in brs:
        ancestor = sh(*g, "merge-base", "--is-ancestor", pin, b)
        if ancestor.returncode == 0:
            keep.add(b)
        elif ancestor.returncode != 1:
            # rc=1 is git's measured "not an ancestor".  Missing objects,
            # corrupt refs, and process failures use other codes and make pin
            # reachability unknown; unknown cannot authorize deletion.
            return None
    return keep


def _automatic_merge_tree_proof(g, commit):
    """Prove one merge commit added no hand-authored merge-only content.

    ``git cherry`` does not describe merge commits.  Treating every merge as
    unique would strand ordinary maintenance branches forever, while ignoring
    every merge can discard conflict-resolution content that exists only in
    the merge commit.  Recreate an ordinary two-parent merge from its recorded
    parents and require the resulting tree to be byte-for-byte the committed
    tree.  A conflict, octopus merge, missing object, older git without
    ``merge-tree --write-tree``, or any different tree is NORECORD.

    ``merge-tree`` writes only unreachable objects, never a ref or worktree.
    That keeps this proof independent of the checked-out branch and means a
    failed proof has nothing to abort or restore.
    """
    proof = {"commit": commit}
    parents = sh(*g, "rev-list", "--parents", "-n", "1", commit)
    proof["parents_rc"] = parents.returncode
    words = (parents.stdout or "").strip().lower().split()
    if (parents.returncode != 0
            or len(words) != 3
            or any(_FULL_OBJECT_ID.fullmatch(word) is None for word in words)
            or words[0] != commit):
        proof.update(
            outcome="norecord", stage="merge_parents",
            detail=("merge does not have one proven pair of parents"
                    if parents.returncode == 0 else
                    "could not read merge parents: "
                    + _first_error_line(parents)),
        )
        return False, proof

    recreated = sh(*g, "merge-tree", "--write-tree", "--no-messages",
                   words[1], words[2])
    proof["merge_tree_rc"] = recreated.returncode
    recreated_words = (recreated.stdout or "").strip().lower().split()
    if (recreated.returncode != 0
            or len(recreated_words) != 1
            or _FULL_OBJECT_ID.fullmatch(recreated_words[0]) is None):
        proof.update(
            outcome="norecord", stage="merge_tree_recreation",
            detail=("parents do not form one clean, reproducible merge tree"
                    if recreated.returncode == 0 else
                    "could not recreate a clean automatic merge: "
                    + _first_error_line(recreated)),
        )
        return False, proof

    actual = sh(*g, "rev-parse", "--verify", "--quiet",
                f"{commit}^{{tree}}")
    proof["committed_tree_rc"] = actual.returncode
    actual_tree = (actual.stdout or "").strip().lower()
    if (actual.returncode != 0
            or _FULL_OBJECT_ID.fullmatch(actual_tree) is None):
        proof.update(
            outcome="norecord", stage="committed_merge_tree",
            detail=("could not read the committed merge tree: "
                    + _first_error_line(actual)),
        )
        return False, proof
    proof.update(recreated_tree=recreated_words[0], committed_tree=actual_tree)
    if recreated_words[0] != actual_tree:
        proof.update(
            outcome="norecord", stage="merge_tree_equivalence",
            detail=("the committed merge tree differs from a clean automatic "
                    "merge of its parents; merge-only content is unmeasured"),
        )
        return False, proof

    proof.update(outcome="complete", method="automatic_merge_tree")
    return True, proof


def _authoritative_origin_mainline(g, local_main):
    """Resolve the freshly fetched fork line corresponding to local main.

    Normally a local ``main`` tracks ``origin/main`` (and likewise for
    ``master``), so the exact-name ref is the least surprising answer.  Some
    old fork clones retain the other conventional local name after the GitHub
    default was renamed, however.  If the exact remote ref is absent, ask the
    remote for its CURRENT HEAD symref and require the freshly fetched tracking
    ref to equal the SHA the remote reports.  A cached ``origin/HEAD`` is not
    evidence: ordinary fetches do not refresh that symref when a default branch
    changes.  Ambiguity, a race, or a missing object stays NORECORD.

    The extra request runs only for the exceptional mismatched-name clone, not
    for every fork.  Its elapsed deadline can produce only NORECORD; it never
    decides that branch work is absent or carried.
    """
    evidence = {"local_main": local_main, "candidates": []}

    def resolved_tip(ref):
        result = sh(*g, "rev-parse", "--verify", "--quiet",
                    f"{ref}^{{commit}}")
        value = (result.stdout or "").strip().lower()
        evidence["candidates"].append({
            "ref": ref, "rc": result.returncode,
            "tip": value if _FULL_OBJECT_ID.fullmatch(value) else None,
        })
        return (value if result.returncode == 0
                and _FULL_OBJECT_ID.fullmatch(value) is not None else None)

    exact = f"origin/{local_main}"
    if resolved_tip(exact):
        evidence.update(outcome="complete", method="matching_remote_name",
                        ref=exact)
        return exact, evidence

    remote_head = sh(*g, "ls-remote", "--symref", "origin", "HEAD",
                     timeout=LS_REMOTE_TIMEOUT_S)
    evidence["remote_head_rc"] = remote_head.returncode
    symbolic_targets = []
    head_tips = []
    for line in (remote_head.stdout or "").splitlines():
        fields = line.strip().split()
        if len(fields) == 3 and fields[0] == "ref:" and fields[2] == "HEAD":
            symbolic_targets.append(fields[1])
        elif (len(fields) == 2 and fields[1] == "HEAD"
              and _FULL_OBJECT_ID.fullmatch(fields[0].lower())):
            head_tips.append(fields[0].lower())
    if (remote_head.returncode == 0 and len(symbolic_targets) == 1
            and len(head_tips) == 1
            and symbolic_targets[0].startswith("refs/heads/")):
        branch = symbolic_targets[0].removeprefix("refs/heads/")
        remote_ref = f"origin/{branch}"
        fetched_tip = resolved_tip(remote_ref)
        evidence.update(remote_head_ref=symbolic_targets[0],
                        remote_head_tip=head_tips[0])
        if fetched_tip == head_tips[0]:
            evidence.update(outcome="complete", method="live_remote_head",
                            ref=remote_ref)
            return remote_ref, evidence

    evidence.update(
        outcome="norecord", stage="origin_mainline",
        detail=("the freshly fetched origin has no unambiguous mainline "
                f"corresponding to local {local_main}"),
    )
    return None, evidence


def _tree_carried(g, target, branch):
    """The commit on ``target`` whose TREE is ``branch``'s tip tree, or None.

    A squash merge publishes every byte of a branch under ONE commit whose
    patch is the sum of the branch's patches.  ``git cherry`` compares patch
    ids one commit at a time, so it reports every commit of a squash-merged
    branch as outstanding.  Measured 2026-09-07 on OpenROAD
    ``postroute-drv-repair``: four commits, tip tree 6d330f6d..., squash commit
    8a484c52 on master with the identical tree; cherry answered ``+ + + +``,
    step 2 merged, conflicted on three files later PRs had changed, and handed
    the gatekeeper AI a case whose only correct verdict -- DECLINE, nothing to
    land -- had no verifiable postcondition.  The round then blocked every
    release, and would have re-handed the same case every morning.

    Tree identity is the stronger fact: not "equivalent patches", but "this
    exact content is on the mainline".  The walk is bounded to the commits
    ``target`` gained since the branch diverged, which is where a squash of it
    can live.

    Returns ``(sha, evidence)``; ``sha`` is None both when no such commit
    exists and when git could not answer -- ``evidence["outcome"]`` separates
    the two, and a caller must not read None as "not carried".
    """
    evidence = {"probe": "tree_identity"}
    tip = sh(*g, "rev-parse", "--verify", "--quiet", f"{branch}^{{tree}}")
    tip_tree = (tip.stdout or "").strip().lower()
    if tip.returncode != 0 or not _FULL_OBJECT_ID.fullmatch(tip_tree):
        evidence.update(outcome="norecord", stage="tip_tree",
                        detail="could not read the branch tip tree: "
                               + _first_error_line(tip))
        return None, evidence
    base = sh(*g, "merge-base", target, branch)
    base_id = (base.stdout or "").strip().lower()
    if base.returncode != 0 or not _FULL_OBJECT_ID.fullmatch(base_id):
        evidence.update(outcome="norecord", stage="merge_base",
                        detail="could not find where the branch diverged: "
                               + _first_error_line(base))
        return None, evidence
    walk = sh(*g, "log", "--format=%H %T", f"{base_id}..{target}")
    if walk.returncode != 0:
        evidence.update(outcome="norecord", stage="tree_walk",
                        detail="could not walk the mainline since divergence: "
                               + _first_error_line(walk))
        return None, evidence
    scanned = 0
    for line in (walk.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        scanned += 1
        if parts[1].lower() == tip_tree:
            evidence.update(outcome="complete", carried_by=parts[0].lower(),
                            tip_tree=tip_tree, scanned=scanned)
            return parts[0].lower(), evidence
    evidence.update(outcome="complete", carried_by=None, tip_tree=tip_tree,
                    scanned=scanned)
    return None, evidence


def _branch_work_against(g, target, branch):
    """Return branch commits whose patches are absent from one mainline.

    ``[]`` is an affirmative answer: the branch tip is already an ancestor of
    ``target``, or every non-merge patch on it is equivalent there. ``None`` is
    NORECORD.  In particular, a failed or malformed ``git cherry`` must never
    turn into the reassuring empty list that the old ``out()`` call produced.

    ``git cherry`` deliberately ignores merge commits.  Its empty output is
    therefore not enough when a non-ancestral merge commit remains in the
    range: such a merge can carry conflict-resolution content of its own.  That
    shape stays unmeasured and cannot authorize skipping the branch or handing
    a manufactured conflict to AI.
    """
    evidence = {"target": target, "branch": branch}
    ancestor = sh(*g, "merge-base", "--is-ancestor", branch, target)
    evidence["ancestry_rc"] = ancestor.returncode
    if ancestor.returncode == 0:
        evidence.update(outcome="complete", method="ancestry",
                        outstanding_count=0)
        return [], evidence
    if ancestor.returncode != 1:
        evidence.update(
            outcome="norecord", stage="ancestry",
            detail=("could not compare branch ancestry: "
                    + _first_error_line(ancestor)),
        )
        return None, evidence

    # A squash-merged branch: its exact tree is on the mainline under a commit
    # git-cherry cannot match.  Checked before cherry because cherry answers
    # "outstanding" for every commit of such a branch and step 2 would then
    # try a merge that can only conflict or duplicate.
    carried_by, tree_check = _tree_carried(g, target, branch)
    evidence["tree_identity"] = tree_check
    if carried_by:
        evidence.update(outcome="complete", method="tree_identity",
                        carried_by=carried_by, outstanding_count=0)
        return [], evidence

    unique, cherry, why = _cherry_probe(g, target, branch)
    evidence["cherry_rc"] = cherry.returncode
    if unique is None:
        evidence.update(
            outcome="norecord", stage="git_cherry",
            detail=why or "git cherry formed no valid answer",
        )
        return None, evidence
    if unique:
        evidence.update(outcome="complete", method="outstanding_patches",
                        outstanding_count=len(unique))
        return unique, evidence

    # No unique non-merge patches.  Prove that the non-ancestral range did not
    # disappear merely because git-cherry omits merge commits.
    merges = sh(*g, "rev-list", "--merges", f"{target}..{branch}")
    evidence["merge_scan_rc"] = merges.returncode
    merge_ids = [line.strip().lower()
                 for line in (merges.stdout or "").splitlines()
                 if line.strip()]
    if merges.returncode != 0:
        evidence.update(
            outcome="norecord", stage="merge_commit_scan",
            detail=("could not inspect non-ancestral merge commits: "
                    + _first_error_line(merges)),
        )
        return None, evidence
    if any(_FULL_OBJECT_ID.fullmatch(commit) is None for commit in merge_ids):
        evidence.update(
            outcome="norecord", stage="merge_commit_scan",
            detail="git rev-list returned malformed merge-commit evidence",
        )
        return None, evidence
    merge_proofs = []
    for commit in merge_ids:
        proven, proof = _automatic_merge_tree_proof(g, commit)
        merge_proofs.append(proof)
        if not proven:
            evidence.update(
                outcome="norecord", stage=proof.get("stage"),
                detail=proof.get(
                    "detail", "merge-only content could not be measured"),
                merge_proofs=merge_proofs,
            )
            return None, evidence

    evidence.update(
        outcome="complete",
        method=("patch_equivalence_with_automatic_merges"
                if merge_proofs else "patch_equivalence"),
        outstanding_count=0,
    )
    if merge_proofs:
        evidence["merge_proofs"] = merge_proofs
    return [], evidence


_FORK_BRANCH_PRS_GRAPHQL = (
    "query($owner:String!,$name:String!,$head:String!){"
    " repository(owner:$owner,name:$name){ nameWithOwner"
    "  pullRequests(headRefName:$head, first:100,"
    "   states:[OPEN,CLOSED,MERGED]){"
    "   totalCount"
    "   nodes{ number url state createdAt mergedAt closedAt"
    "          headRefName headRefOid headRepository{ nameWithOwner } } } } }")


def _fork_branch_pull_requests(g, branch):
    """Every pull request ON THE FORK whose head is `branch`, asked of GitHub.

    WHY CONSOLIDATION NEEDS THIS (measured 2026-09-27)
    --------------------------------------------------
    The 09:17 round merged `origin/next/claude-mig104-fill-area` and
    `origin/next/claude-mig105-signoff-not-silent` into librelane's main and
    pushed. Both were the heads of OPEN pull requests on vibeic/librelane --
    #17 under a fork-gatekeeper HOLD review for a DRC/LVS defect, #16 with its
    verification still running -- and GitHub marked both MERGED at
    01:23:43Z because their commits had arrived on the base by another road.

    The selection rule was the 2026-07-31 one: any origin branch that holds a
    patch of ours absent from BOTH mainlines is "stranded work" and gets merged.
    It predates PRs on the forks. The owner's ruling it implements says the
    branch exists "為了要驗證某一個 PR 有沒有結束，結束了以後" -- the commits
    come in AFTER the PR is done. A branch whose PR is still open is not
    stranded; it is waiting on the gatekeeper, and the PR is where that decision
    is made. Git cannot answer "is there a PR", so GitHub is asked -- through
    GraphQL, because REST/search have returned empty sets for this org.

    Returns ``{"outcome": "complete", "prs": [...]}`` or
    ``{"outcome": "norecord", "reason": ...}``. NORECORD is never "no PRs":
    the caller refuses to merge on it.
    """
    head = branch.removeprefix("origin/")
    origin_url = out(*g, "remote", "get-url", "origin")
    slug = _remote_slug(origin_url)

    def _norecord(reason, **extra):
        row = {"outcome": "norecord", "repository": slug or None,
               "head_ref": head, "prs": None, "reason": reason}
        row.update(extra)
        return row

    if not slug or not _is_github_remote(origin_url):
        return _norecord("origin is not a GitHub repository; a local remote is "
                         "not evidence that the branch has no pull request")
    owner, name = slug.split("/", 1)
    result = sh("gh", "api", "graphql",
                "-f", f"owner={owner}", "-f", f"name={name}",
                "-f", f"head={head}",
                "-f", f"query={_FORK_BRANCH_PRS_GRAPHQL}",
                timeout=GITHUB_API_TIMEOUT_S)
    if result.returncode != 0:
        return _norecord(f"GitHub GraphQL failed: {_first_error_line(result)}",
                         rc=result.returncode)
    try:
        body = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError):
        return _norecord("GitHub GraphQL answer is not complete JSON")
    if not isinstance(body, dict) or body.get("errors"):
        return _norecord("GitHub GraphQL answered with errors: "
                         f"{str((body or {}).get('errors'))[:200]}")
    repo = (body.get("data") or {}).get("repository")
    if not isinstance(repo, dict):
        return _norecord("GitHub GraphQL did not resolve the repository")
    if str(repo.get("nameWithOwner", "")).lower() != slug.lower():
        return _norecord(f"GitHub answered for {repo.get('nameWithOwner')!r}, "
                         f"not {slug!r}")
    conn = repo.get("pullRequests")
    if not isinstance(conn, dict):
        return _norecord("pullRequests connection missing")
    total, nodes = conn.get("totalCount"), conn.get("nodes")
    if type(total) is not int or not isinstance(nodes, list):
        return _norecord("pullRequests connection malformed")
    if total != len(nodes):
        # More than one page (or a truncated one). An unread page may hold the
        # open PR; partial evidence is not evidence.
        return _norecord(f"{total} pull request(s) exist, {len(nodes)} read")
    prs = []
    for node in nodes:
        if not isinstance(node, dict):
            return _norecord("pull request node is not an object")
        number, state = node.get("number"), node.get("state")
        if type(number) is not int or number <= 0:
            return _norecord("pull request number missing")
        if state not in ("OPEN", "CLOSED", "MERGED"):
            return _norecord(f"pull request #{number} has state {state!r}")
        if node.get("headRefName") != head:
            return _norecord(f"pull request #{number} head is "
                             f"{node.get('headRefName')!r}, not {head!r}")
        head_repo = node.get("headRepository")
        head_slug = (head_repo.get("nameWithOwner")
                     if isinstance(head_repo, dict) else None)
        # A PR from ANOTHER repository that happens to use the same branch
        # name is not about our branch. A deleted head repository cannot be
        # told apart from ours, so it is kept: holding a merge on a PR that
        # might be ours costs a day; merging past one costs the review.
        if isinstance(head_slug, str) and head_slug.lower() != slug.lower():
            continue
        prs.append({"number": number, "state": state, "url": node.get("url"),
                    "created_at": node.get("createdAt"),
                    "merged_at": node.get("mergedAt"),
                    "closed_at": node.get("closedAt"),
                    "head_oid": node.get("headRefOid")})
    return {"outcome": "complete", "repository": slug, "head_ref": head,
            "prs": prs, "via": "graphql"}


def consolidation_verdict(evidence):
    """Whether a branch's PR state lets consolidation merge it.

    Returns ``(verdict, detail)``:

    * ``"merge"``      -- no pull request ever had this head (lane work nobody
                          put up for review: the 2026-07-31 consolidation
                          rule), or its newest one was MERGED (the PR is done;
                          "結束了以後" its commits come in).
    * ``"open_pr"``    -- a PR with this head is OPEN. The PR is the
                          gatekeeper's decision point -- HOLD, verification in
                          progress, or simply unreviewed -- and a merge here
                          would take that decision away from it.
    * ``"declined_pr"``-- the newest PR was CLOSED without merging. Somebody
                          said no (or superseded it); merging the branch would
                          reverse that.
    * ``"unknown"``    -- PR state could not be determined. Refuse, loudly.
    """
    if not isinstance(evidence, dict) or evidence.get("outcome") != "complete":
        reason = (evidence or {}).get("reason") if isinstance(
            evidence, dict) else None
        return "unknown", reason or "pull request state not measured"
    prs = evidence.get("prs")
    if not isinstance(prs, list):
        return "unknown", "pull request list missing"
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    if open_prs:
        return "open_pr", ", ".join(f"#{p['number']}" for p in open_prs)
    if not prs:
        return "merge", "no pull request has this head"
    newest = max(prs, key=lambda p: p["number"])
    if newest.get("state") == "MERGED":
        return "merge", f"#{newest['number']} merged"
    return "declined_pr", f"#{newest['number']} closed without merging"


def step2_ours(g, main, rep):
    """Our branches -> master, against local and authoritative remote lines.

    Only branches whose pull-request state allows it are merged: a branch that
    heads an OPEN PR on the fork, or whose newest PR was closed unmerged, is
    left for the PR to decide (see `consolidation_verdict`), and a branch whose
    PR state cannot be read is REFUSED with `needs_human` -- never merged.
    """
    brs = _fork_branches(g, main)
    if brs is None:
        # NOT `[]`. An unanswerable question rendered as "this fork has no
        # branches" is how work on a fork the round could not read gets reported
        # as a fork with nothing to consolidate.
        #
        # `ours_skipped` is named for `prune_skipped` below, which is the shape
        # this file already uses for "the step declined and here is why", and it
        # rides with `needs_human` — the flag `main_` turns into the round's exit
        # code. `merged` is stated as empty because that is TRUE and consumers
        # read it; `carried_nothing_new` is deliberately NOT set, because how
        # many branches carried nothing new is exactly the number this step could
        # not compute, and writing 0 there would publish a measurement that was
        # never taken. `conflicted` uses setdefault for the same reason it does
        # below: step1 runs first and may already have put one there.
        rep["ours_skipped"] = ("BRANCH LIST UNKNOWN — the fetch of origin did "
                               "not succeed, so which branches this fork has "
                               "could not be read. Nothing was merged, and that "
                               "is NOT the same as nothing to merge.")
        rep["merged"] = []
        rep.setdefault("conflicted", [])
        rep["needs_human"] = True
        return
    merged, conflicted, empty = [], [], []
    remote_main = None
    remote_main_evidence = None
    for b in brs:
        checks = []
        local_unique, local_check = _branch_work_against(g, main, b)
        checks.append(local_check)
        if local_unique == []:
            empty.append(b)
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "already_carried",
                "covered_by": main, "checks": checks,
            })
            continue

        # `_fork_branches` refreshed origin immediately before returning this
        # list.  Its mainline ref is therefore the authoritative fork state for
        # this round, while the checked-out local main may predate a merge that
        # already delivered the branch.  Measured on iverilog: local master was
        # 93b302f, origin/master was 5d87083, and source tip 14d106f was already
        # its ancestor. Comparing only to local manufactured a two-commit
        # conflict and an unnecessary AI case; the same-tree repair later
        # adopted the remote and proved that work had been present all along.
        if remote_main_evidence is None:
            remote_main, remote_main_evidence = (
                _authoritative_origin_mainline(g, main))
            rep["ours_remote_mainline"] = remote_main_evidence
        if remote_main is None:
            remote_unique = None
            remote_check = dict(remote_main_evidence)
            remote_check["target"] = None
            remote_check["branch"] = b
        else:
            remote_unique, remote_check = _branch_work_against(
                g, remote_main, b)
            remote_check["mainline_selection"] = remote_main_evidence
        checks.append(remote_check)
        if remote_unique == []:
            empty.append(b)
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "already_carried",
                "covered_by": remote_main, "checks": checks,
            })
            continue

        if local_unique is None or remote_unique is None:
            failed = next(check for check in checks
                          if check.get("outcome") != "complete")
            rep.setdefault("merge_unmeasurable", []).append({
                "branch": b,
                "commits": None,
                "outcome": "unmeasurable",
                "rc": (failed.get("cherry_rc")
                       if failed.get("stage") == "git_cherry"
                       else failed.get("ancestry_rc")
                       if failed.get("stage") == "ancestry"
                       else failed.get("merge_scan_rc")),
                "stage": failed.get("stage"),
                "detail": failed.get("detail"),
                "comparisons": checks,
            })
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "norecord", "checks": checks,
            })
            rep["needs_human"] = True
            continue

        # A patch carried by either mainline is not truly stranded branch work.
        # Only commits absent from BOTH are candidates for consolidation.
        remote_absent = set(remote_unique)
        new = [commit for commit in local_unique if commit in remote_absent]
        if not new:
            empty.append(b)
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "already_carried",
                "covered_by": f"{main} or {remote_main}", "checks": checks,
            })
            continue
        # only OUR commits are a reason to merge; an unmerged upstream commit
        # on a stale branch is not work, it is history.
        mine = []
        author_unknown = None
        for commit in new:
            author = sh(*g, "log", "-1", "--format=%an", commit)
            author_name = (author.stdout or "").strip().lower()
            if author.returncode != 0 or not author_name:
                author_unknown = {
                    "commit": commit, "rc": author.returncode,
                    "detail": (_first_error_line(author)
                               if author.returncode else
                               "git log returned an empty author"),
                }
                break
            if any(owner in author_name for owner in OURS):
                mine.append(commit)
        if author_unknown is not None:
            rep.setdefault("merge_unmeasurable", []).append({
                "branch": b, "commits": None, "outcome": "unmeasurable",
                "rc": author_unknown["rc"], "stage": "commit_author",
                "detail": author_unknown["detail"],
                "commit": author_unknown["commit"],
                "comparisons": checks,
            })
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "norecord", "checks": checks,
            })
            rep["needs_human"] = True
            continue
        if not mine:
            empty.append(b)
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": "no_owned_work", "checks": checks,
            })
            continue
        # THE PR, NOT GIT, DECIDES WHETHER THIS BRANCH IS DONE. Asked only for
        # a branch that would otherwise merge, so a quiet fork costs no API
        # call; asked BEFORE the merge, so nothing is undone afterwards.
        pr_evidence = _fork_branch_pull_requests(g, b)
        verdict, why = consolidation_verdict(pr_evidence)
        if verdict != "merge":
            outcome = {"open_pr": "held_by_open_pr",
                       "declined_pr": "held_by_declined_pr",
                       "unknown": "refused_pr_state_unknown"}[verdict]
            rep.setdefault("ours_branch_evidence", []).append({
                "branch": b, "outcome": outcome, "commits": len(mine),
                "reason": why, "pull_requests": pr_evidence, "checks": checks,
            })
            rep.setdefault("ours_held", []).append({
                "branch": b, "commits": len(mine), "verdict": verdict,
                "reason": why,
            })
            if verdict == "unknown":
                # FAIL LOUDLY. Silence here was the old behaviour's whole
                # defect in a different costume: an unanswered question read
                # as permission.
                print(f"  [REFUSED] {b}: pull-request state unknown ({why}); "
                      f"not merged", file=sys.stderr)
                # Also `merge_unmeasurable`: whether this branch conflicts was
                # never measured, and the release authorizer already refuses
                # a round carrying that key -- so the refusal reaches the
                # release gate, not only this round's exit code.
                rep.setdefault("merge_unmeasurable", []).append({
                    "branch": b, "commits": len(mine),
                    "outcome": "unmeasurable", "rc": pr_evidence.get("rc")
                    if isinstance(pr_evidence, dict) else None,
                    "stage": "pull_request_state", "detail": why,
                })
                rep["needs_human"] = True
            continue
        rep.setdefault("ours_branch_evidence", []).append({
            "branch": b, "outcome": "outstanding",
            "commits": len(mine), "checks": checks,
            "pull_requests": pr_evidence,
        })
        r = sh(*g, "merge", "--no-edit", "-m",
               f"consolidate: bring {b} onto {main} (one line, no long-lived branches)", b,
               progress=True)
        if r.returncode:
            # Collect the evidence a merge-vs-cherry-pick decision needs BEFORE
            # aborting, because aborting destroys it. The owner's standing rule
            # is that this decision needs AI: "Merge 或者是 Cherry-pick 的話,
            # 是需要用到 AI 的, 用程式直接判斷是不夠的." A script that stops
            # here and prints needs_human has not made the decision, it has
            # only deferred it — and in practice nobody came back for it.
            files = [l for l in out(*g, "diff", "--name-only",
                                    "--diff-filter=U").splitlines() if l.strip()]
            if r.returncode == watchdog.RC_STALLED or not files:
                sh(*g, "merge", "--abort")
                rep.setdefault("merge_unmeasurable", []).append({
                    "branch": b,
                    "commits": len(mine),
                    "outcome": ("stalled" if
                                r.returncode == watchdog.RC_STALLED
                                else "natural_failure"),
                    "rc": r.returncode,
                    "detail": (
                        "no forward progress; UNKNOWN, not a conflict"
                        if r.returncode == watchdog.RC_STALLED
                        else _first_error_line(r)),
                })
                rep["needs_human"] = True
                continue
            commits = [{"sha": s[:12],
                        "subject": out(*g, "log", "-1", "--format=%s", s),
                        "author": out(*g, "log", "-1", "--format=%an", s),
                        "files": [x for x in out(*g, "show", "--name-only",
                                                 "--format=", s).splitlines()
                                  if x.strip()][:20]}
                       for s in mine[:20]]
            sh(*g, "merge", "--abort")
            conflicted.append({
                "branch": b,
                # STATED, not defaulted. The brief renders this per case and the
                # two directions put OPPOSITE sides at risk, so a case that does
                # not say which it is gets decided on a guess.
                "direction": "our branch -> our mainline",
                "commits": len(mine),
                "conflicting_files": files,
                "commits_detail": commits,
                "merge_stderr": (r.stderr or r.stdout or "").strip()[-800:],
            })
        else:
            merged.append({"branch": b, "commits": len(mine)})
    rep["merged"] = merged
    # APPEND, never assign: step1 may already have put an upstream conflict here,
    # and step1 runs first. Assigning would drop the very conflicts that put the
    # "upstream is all in" invariant at risk -- and it would do it silently.
    rep.setdefault("conflicted", []).extend(conflicted)
    rep["carried_nothing_new"] = len(empty)


def _remote_slug(url):
    """Return ``owner/repository`` for the Git remote spellings we accept."""
    u = (url or "").strip().removesuffix(".git")
    if u.startswith("git@") and ":" in u:
        u = u.split(":", 1)[-1]
    parts = [p for p in u.split("/") if p]
    slug = "/".join(parts[-2:]) if len(parts) >= 2 else ""
    return slug if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug) else ""


def _is_github_remote(url):
    u = (url or "").strip().lower()
    return (u.startswith("git@github.com:")
            or re.match(r"^(https?|git|ssh)://(?:git@)?github\.com/", u)
            is not None)


def _pr_norecord(upstream, owner, reason, *, rc=None):
    """The one fail-closed shape for incomplete upstream-PR evidence."""
    row = {
        "outcome": "norecord",
        "upstream_repository": upstream or None,
        "fork_owner": owner or None,
        "open_count": None,
        "items": [],
        "reason": reason,
    }
    if rc is not None:
        row["rc"] = rc
    return row


def _is_http_404(result):
    """gh prints `gh: Not Found (HTTP 404)` and the API body's `"status": "404"`."""
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    return "(HTTP 404)" in text or '"status": "404"' in text or '"status":"404"' in text


_UPSTREAM_PRS_GRAPHQL = (
    "query($owner:String!,$name:String!,$endCursor:String){"
    " repository(owner:$owner,name:$name){ nameWithOwner"
    "  pullRequests(states:OPEN, first:100, after:$endCursor){"
    "   pageInfo{ hasNextPage endCursor }"
    "   nodes{ number url title isDraft updatedAt headRefName baseRefName"
    "          state headRepositoryOwner{ login } } } } }")


def _upstream_pull_request_evidence_graphql(upstream, owner):
    """The REST question, asked of GraphQL: every OPEN PR on `upstream`, then
    the ones whose head repository is ours.  Returns the same envelope as
    `_parse_upstream_pr_pages`, with `via: "graphql"` so the record says how it
    was measured, or None when GraphQL could not answer completely -- the
    caller then records NORECORD with the REST reason.  Never a zero from an
    incomplete or malformed page: the same rules the REST parser applies."""
    try:
        gh_owner, gh_name = upstream.split("/", 1)
    except ValueError:
        return None
    result = sh("gh", "api", "graphql", "--paginate", "--slurp",
                "-f", f"owner={gh_owner}", "-f", f"name={gh_name}",
                "-f", f"query={_UPSTREAM_PRS_GRAPHQL}",
                timeout=GITHUB_API_TIMEOUT_S)
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(pages, list) or not pages:
        return None
    items, seen = [], set()
    for page_i, page in enumerate(pages):
        repo = (((page or {}).get("data") or {}).get("repository")
                if isinstance(page, dict) else None)
        if not isinstance(repo, dict):
            return None                      # repository did not resolve
        name = repo.get("nameWithOwner")
        if not isinstance(name, str) or name.lower() != upstream.lower():
            return None
        prs = repo.get("pullRequests")
        if not isinstance(prs, dict):
            return None
        info, nodes = prs.get("pageInfo"), prs.get("nodes")
        if not isinstance(info, dict) or not isinstance(nodes, list):
            return None
        if len(nodes) > 100:
            return None
        last = page_i == len(pages) - 1
        if info.get("hasNextPage") is not False and last:
            return None                      # pagination did not finish
        if not last and (len(nodes) != 100 or info.get("hasNextPage") is not True):
            return None                      # a non-final page must be full
        for node in nodes:
            if not isinstance(node, dict):
                return None
            number = node.get("number")
            url, title = node.get("url"), node.get("title")
            draft, updated = node.get("isDraft"), node.get("updatedAt")
            head_ref, base_ref = node.get("headRefName"), node.get("baseRefName")
            login = ((node.get("headRepositoryOwner") or {}).get("login")
                     if isinstance(node.get("headRepositoryOwner"), dict) else None)
            if type(number) is not int or number <= 0 or number in seen:
                return None
            seen.add(number)
            if (not isinstance(url, str) or not url or not isinstance(title, str)
                    or node.get("state") != "OPEN" or type(draft) is not bool
                    or not isinstance(updated, str) or not updated
                    or not isinstance(head_ref, str) or not head_ref
                    or not isinstance(base_ref, str) or not base_ref):
                return None
            try:
                stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                return None
            if stamp.tzinfo is None:
                return None
            # A deleted head repository has no owner; that PR cannot be ours.
            if not isinstance(login, str) or login.lower() != owner.lower():
                continue
            items.append({
                "number": number, "url": url, "title": title,
                "head_ref": head_ref, "base_ref": base_ref,
                "draft": draft, "updated_at": updated,
            })
    return {
        "outcome": "complete",
        "upstream_repository": upstream,
        "fork_owner": owner,
        "open_count": len(items),
        "items": items,
        "via": "graphql",
        "rest_status": 404,
    }


def _parse_upstream_pr_pages(raw, upstream, owner):
    """Validate and flatten ``gh api --paginate --slurp`` evidence.

    A flat list is the old first-page-only shape and is rejected.  Every page,
    every PR, and every field used by the deletion guard must be present with
    its API type.  Partial evidence is NORECORD; it is never equivalent to
    "there are no matching PRs".
    """
    try:
        pages = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"response is not complete JSON: {exc}") from exc
    if not isinstance(pages, list) or not pages:
        raise ValueError("paginated response must be a non-empty list of pages")
    if any(not isinstance(page, list) for page in pages):
        raise ValueError("paginated response is not an array of page arrays")
    # `gh api --paginate` follows GitHub's Link header; it does not need a
    # synthetic empty page to terminate.  Accepting `[100 rows, []]` would let
    # a fabricated/truncated second page turn 100 foreign PRs into an asserted
    # zero for our owner.  The one legitimate empty result is the single `[[]]`
    # response for a repository with no open PRs.
    if len(pages) > 1 and not pages[-1]:
        raise ValueError("paginated response has a synthetic empty final page")

    items, seen = [], set()
    for page_i, page in enumerate(pages):
        if len(page) > 100:
            raise ValueError(f"page {page_i + 1} exceeds per_page=100")
        if page_i < len(pages) - 1 and len(page) != 100:
            raise ValueError(f"non-final page {page_i + 1} is partial")
        for item_i, pr in enumerate(page):
            where = f"page {page_i + 1} item {item_i + 1}"
            if not isinstance(pr, dict):
                raise ValueError(f"{where} is not an object")
            number = pr.get("number")
            url = pr.get("html_url")
            title = pr.get("title")
            state = pr.get("state")
            draft = pr.get("draft")
            updated = pr.get("updated_at")
            head, base = pr.get("head"), pr.get("base")
            if type(number) is not int or number <= 0:       # bool is not an id
                raise ValueError(f"{where}.number is not a positive integer")
            if number in seen:
                raise ValueError(f"pull request #{number} occurs more than once")
            seen.add(number)
            if not isinstance(url, str) or not url:
                raise ValueError(f"{where}.html_url is missing")
            if not isinstance(title, str):
                raise ValueError(f"{where}.title is missing")
            if state != "open":
                raise ValueError(f"{where}.state is not open")
            if type(draft) is not bool:
                raise ValueError(f"{where}.draft is not boolean")
            if not isinstance(updated, str) or not updated:
                raise ValueError(f"{where}.updated_at is missing")
            try:
                stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{where}.updated_at is malformed") from exc
            if stamp.tzinfo is None:
                raise ValueError(f"{where}.updated_at has no timezone")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise ValueError(f"{where}.head/base is missing")
            head_ref, base_ref = head.get("ref"), base.get("ref")
            repo = head.get("repo")
            login = (((repo or {}).get("owner") or {}).get("login")
                     if isinstance(repo, dict) else None)
            if not isinstance(head_ref, str) or not head_ref:
                raise ValueError(f"{where}.head.ref is missing")
            if not isinstance(base_ref, str) or not base_ref:
                raise ValueError(f"{where}.base.ref is missing")
            if not isinstance(login, str) or not login:
                raise ValueError(f"{where}.head.repo.owner.login is missing")
            if login.lower() != owner.lower():
                continue
            items.append({
                "number": number,
                "url": url,
                "title": title,
                "head_ref": head_ref,
                "base_ref": base_ref,
                "draft": draft,
                "updated_at": updated,
            })
    return {
        "outcome": "complete",
        "upstream_repository": upstream,
        "fork_owner": owner,
        "open_count": len(items),
        "items": items,
    }


def _upstream_pull_request_evidence(g):
    """Return exact, retained evidence for our open PRs against upstream."""
    origin = out(*g, "remote", "get-url", "origin")
    upstream_url = out(*g, "remote", "get-url", "upstream")
    upstream = _remote_slug(upstream_url)
    origin_slug = _remote_slug(origin)
    owner = origin_slug.split("/", 1)[0] if origin_slug else ""
    if not upstream or not owner:
        return _pr_norecord(
            upstream, owner,
            "could not derive upstream repository and fork owner from remotes")
    upstream_is_github = _is_github_remote(upstream_url)
    origin_is_github = _is_github_remote(origin)
    if not upstream_is_github or not origin_is_github:
        return _pr_norecord(
            upstream, owner,
            "origin and upstream are not both GitHub repositories; a local "
            "remote is not evidence that GitHub has zero open pull requests")
    if owner.lower() != FORK_OWNER:
        return _pr_norecord(
            upstream, owner,
            f"origin owner is {owner!r}, not the managed {FORK_OWNER!r}; "
            "refusing to use another owner's PRs as destructive evidence")

    endpoint = f"repos/{upstream}/pulls?state=open&per_page=100"
    result = sh("gh", "api", endpoint, "--paginate", "--slurp",
                timeout=GITHUB_API_TIMEOUT_S)
    if result.returncode != 0:
        # A REST 404 on /pulls for a repository that otherwise resolves is not
        # "no pull requests" and it is not "the repository is gone" either.
        # Measured 2026-09-07 on IHP-GmbH/ihp-sg13cmos5l (the new cmos5l pin):
        # `repos/.../pulls` -> 404 for state=open, state=all and a single PR
        # number, while `repos/...` , `/commits`, `/issues` and GraphQL all
        # answer, and GraphQL reports 0 open PRs.  Sibling repos of the same
        # org answer REST normally.  Left as-is the fork was NORECORD every
        # round: prune skipped, needs_human, and the page gate held open on a
        # fork that has nothing pending.  So a 404 -- and only a 404 -- is
        # answered by the SAME question put to GraphQL, which is a measurement
        # of the same thing and is parsed under the same fail-closed rules.
        # Any other failure, and any GraphQL failure, stays NORECORD.
        if _is_http_404(result):
            evidence = _upstream_pull_request_evidence_graphql(upstream, owner)
            if evidence is not None:
                return evidence
        return _pr_norecord(
            upstream, owner,
            f"GitHub pagination failed: {_first_error_line(result)}",
            rc=result.returncode)
    try:
        return _parse_upstream_pr_pages(result.stdout, upstream, owner)
    except ValueError as exc:
        return _pr_norecord(
            upstream, owner, f"GitHub pagination evidence invalid: {exc}",
            rc=result.returncode)


def _branches_serving_an_open_upstream_pr(g, evidence=None):
    """Branch names that an OPEN pull request upstream uses as its head.

    `git cherry` answers "is this work already in main". That is the right
    question for OUR line and the wrong question for a branch that is also the
    head of a PR we filed UPSTREAM: once we merge our own fix into our fork's
    master, cherry reports nothing unique, the branch is pruned, and deleting
    the head branch CLOSES THE PULL REQUEST. The work survives on our line and
    the contribution silently dies.

    Measured, 2026-07-31: steveicarus/iverilog#1455 (the non-blocking event
    trigger fix) was closed at 04:33 UTC by exactly this path -- closed and
    head_ref_deleted in the same second, by us, with no maintainer involved. It
    was the only upstream PR we had.

    So ask GitHub, not git. Failure or incomplete pagination returns None, and
    the caller then prunes NOTHING: a branch kept by mistake costs a line of
    clutter, a branch deleted by mistake costs an upstream contribution.

    The optional evidence argument lets ``step4_prune`` reuse the structured
    row already retained by ``main_``.  Calling this historical helper with
    only ``g`` remains supported and still returns ``set``/``None``.
    """
    if evidence is None:
        evidence = _upstream_pull_request_evidence(g)
    if (not isinstance(evidence, dict)
            or evidence.get("outcome") != "complete"):
        return None
    items = evidence.get("items")
    count = evidence.get("open_count")
    if (not isinstance(evidence.get("upstream_repository"), str)
            or not evidence.get("upstream_repository")
            or not isinstance(evidence.get("fork_owner"), str)
            or not evidence.get("fork_owner")
            or evidence.get("fork_owner", "").lower() != FORK_OWNER
            or type(count) is not int or count < 0
            or not isinstance(items, list) or count != len(items)):
        return None
    refs = set()
    for item in items:
        if (not isinstance(item, dict)
                or not isinstance(item.get("head_ref"), str)
                or not item.get("head_ref")):
            return None
        refs.add(item["head_ref"])
    return refs


def _is_ours_to_delete(g, b):
    """A branch WE created as PR scaffolding, not one the fork inherited.

    Reading the fork's real branch list (rather than the local clone's) is what
    makes the consolidation honest, and it also hands `prune` 138 branches on
    yosys instead of 9 — almost all of them upstream's own history, copied into
    the fork when it was created. Deleting those is not "分支太多" cleanup; it
    is throwing away a public project's branches from our mirror of it.

    So deletion is scoped to what we made: a `vibeic/` branch, or a branch whose
    tip we authored. Anything inherited is left alone whatever `git cherry`
    says about it.
    """
    name = b.split("/", 1)[-1] if b.startswith("origin/") else b
    if name.startswith("vibeic/") or b.startswith("origin/vibeic/"):
        return True
    au = out(*g, "log", "-1", "--format=%an <%ae>", b).lower()
    return any(o in au for o in OURS)


_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _ref_tip(g, ref):
    """Return one proven full commit id and the command evidence.

    Abbreviated ids are unsuitable for `--force-with-lease`: the full expected
    remote value is the deletion boundary's compare-and-swap token.
    """
    result = sh(*g, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    value = (result.stdout or "").strip().lower()
    if result.returncode != 0 or not _FULL_OBJECT_ID.fullmatch(value):
        return None, result
    return value, result


def _cherry_probe(g, main, branch):
    """Return (unique commits, command, reason); unknown is never empty."""
    result = sh(*g, "cherry", main, branch)
    if result.returncode != 0:
        return None, result, _first_error_line(result)
    lines = [line.strip() for line in (result.stdout or "").splitlines()
             if line.strip()]
    if any(re.fullmatch(r"[+-] [0-9a-fA-F]{40,64}(?: .*)?", line) is None
           for line in lines):
        return None, result, "git cherry returned malformed output"
    return [line.split()[1].lower() for line in lines
            if line.startswith("+")], result, ""


def _prune_failure(rep, row, stage, reason):
    """Retain a fail-closed destructive-boundary outcome in the round."""
    row.update({"outcome": "retained", "blocked_at": stage, "reason": reason})
    rep.setdefault("prune_rechecks", []).append(row)
    rep.setdefault("prune_retained", []).append({
        "branch": row.get("branch"),
        "expected_tip": row.get("expected_tip"),
        "blocked_at": stage,
        "reason": reason,
    })
    rep["needs_human"] = True


def _worktree_branches(g):
    """Measure every registered worktree's branch; unknown is never empty.

    Local deletion is BLOCKED on unreadable or malformed inventory. Locked and
    prunable/missing worktrees still own their branch. NUL records preserve
    whitespace in paths. Detached entries are retained separately: they may
    reserve an original branch during rebase/bisect that porcelain does not name.
    """
    result = sh(*g, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        return None, result, _first_error_line(result)
    raw = result.stdout or ""
    branches = {}
    if not raw.endswith("\0\0"):
        return None, result, "worktree inventory is empty or unterminated"
    for record in raw[:-2].split("\0\0"):
        fields = {}
        for field in record.split("\0"):
            key, _, value = field.partition(" ")
            if key in fields or key not in {
                    "worktree", "HEAD", "branch", "detached", "bare",
                    "locked", "prunable"}:
                return None, result, "worktree inventory has invalid fields"
            fields[key] = value
        if not record.startswith("worktree ") or not fields.get("worktree"):
            return None, result, "worktree inventory has no path"
        if any(fields.get(flag) for flag in ("bare", "detached")):
            return None, result, "worktree inventory has invalid state flags"
        if "bare" in fields:
            valid = not any(k in fields for k in ("HEAD", "branch", "detached"))
        else:
            valid = (bool(_FULL_OBJECT_ID.fullmatch(fields.get("HEAD", "")))
                     and (("branch" in fields) != ("detached" in fields)))
        if not valid:
            return None, result, "worktree inventory has ambiguous HEAD binding"
        if "branch" in fields:
            branch = fields["branch"]
            if not branch.startswith("refs/heads/") or not branch[11:]:
                return None, result, "worktree inventory has invalid branch binding"
            branches.setdefault(branch, []).append(fields["worktree"])
        elif "detached" in fields:
            branches.setdefault("detached", []).append(fields["worktree"])
    return branches, result, ""


def step4_prune(g, main, rep, apply):
    """Delete only what git itself says holds nothing unique -- AND what is not
    serving an open upstream PR -- AND what we created in the first place."""
    listed = _fork_branches(g, main)
    if listed is None:
        # Same fail-safe the two guards below already use, for the same class of
        # reason: a DELETE decided from a branch list we could not read is the
        # most expensive thing this step can get wrong.
        rep["pruned"] = []
        rep["prune_skipped"] = ("could not list the fork's branches (the fetch "
                                "of origin did not succeed); pruned nothing "
                                "rather than delete against a stale listing")
        rep["needs_human"] = True
        return
    evidence = rep.get("upstream_pull_requests")
    if not isinstance(evidence, dict):
        evidence = _upstream_pull_request_evidence(g)
        rep["upstream_pull_requests"] = evidence
    protected = _branches_serving_an_open_upstream_pr(g, evidence)
    if protected is None:
        rep["pruned"] = []
        reason = evidence.get("reason", "incomplete upstream PR evidence")
        rep["prune_skipped"] = (
            f"upstream PR evidence is NORECORD ({reason}); pruned nothing "
            "rather than risk closing an open contribution")
        rep["needs_human"] = True
        return
    brs = [b for b in listed if _is_ours_to_delete(g, b)]
    pinned = _branches_an_image_pin_depends_on(g)
    if pinned is None:
        rep["pruned"] = []
        rep["prune_skipped"] = ("could not read the image pins; pruned nothing "
                                "rather than risk orphaning one")
        rep["needs_human"] = True
        return
    if pinned:
        rep["prune_pin_protected"] = sorted(pinned)
    brs = [b for b in brs if b not in pinned]
    if protected:
        rep["prune_protected"] = sorted(protected)
    candidates = []
    planning_failures = []
    for b in brs:
        if (b in protected
                or b.split("/", 1)[-1] in protected):
            continue                      # head of an open upstream PR — keep
        unique, cherry, why = _cherry_probe(g, main, b)
        if unique is None:
            planning_failures.append({
                "branch": b, "stage": "git_cherry", "rc": cherry.returncode,
                "detail": why,
            })
            continue
        if unique:
            continue                      # still holds unique work — keep
        expected_tip, tip_read = _ref_tip(g, b)
        if expected_tip is None:
            planning_failures.append({
                "branch": b, "stage": "branch_tip",
                "rc": tip_read.returncode,
                "detail": (_first_error_line(tip_read)
                           if tip_read.returncode else
                           "rev-parse did not return a full object id"),
            })
            continue
        candidates.append((b, expected_tip))

    # One unmeasured candidate makes the destructive plan incomplete.  Do not
    # partially execute a plan whose branch set was not fully classified.
    if planning_failures:
        rep["pruned"] = []
        rep["prune_plan_blocked"] = planning_failures
        rep["prune_skipped"] = (
            "branch deletion planning was incomplete; git cherry or the "
            "candidate tip could not be proved, so nothing was deleted")
        rep["needs_human"] = True
        return

    if not apply:
        rep["pruned"] = [branch for branch, _tip in candidates]
        rep["prune_candidate_tips"] = {
            branch: tip for branch, tip in candidates}
        return

    gone = []
    for b, expected_tip in candidates:
        local = b.split("/", 1)[-1] if b.startswith("origin/") else b
        boundary = {"branch": b, "expected_tip": expected_tip}

        # Everything that justified the plan can change while merge/check/push
        # runs.  Fetch first, then re-prove every predicate.  The final push is
        # still protected by the expected SHA lease, so a remote move after
        # this fetch cannot be deleted by the race winner.
        fetched, fetch_why = _fetch(g, "origin", "--prune")
        boundary["fetch"] = "complete" if fetched else "norecord"
        if not fetched:
            _prune_failure(rep, boundary, "fresh_fetch", fetch_why)
            continue

        observed_tip, tip_read = _ref_tip(g, b)
        boundary["observed_tip"] = observed_tip
        if observed_tip is None:
            _prune_failure(
                rep, boundary, "branch_exists",
                f"fresh origin ref is absent or unreadable (rc={tip_read.returncode}: "
                f"{_first_error_line(tip_read)})")
            continue
        if observed_tip != expected_tip:
            _prune_failure(
                rep, boundary, "branch_tip",
                f"branch moved from {expected_tip} to {observed_tip}")
            continue

        # Re-read Dockerfile pins at the destructive boundary.  This helper
        # refreshes origin while classifying reachability, so prove the branch
        # SHA once more after it returns.
        fresh_pinned = _branches_an_image_pin_depends_on(g)
        if fresh_pinned is None:
            _prune_failure(rep, boundary, "image_pins",
                           "image pin dependencies became unmeasurable")
            continue
        observed_after_pins, tip_read = _ref_tip(g, b)
        boundary["observed_tip_after_pins"] = observed_after_pins
        if observed_after_pins is None or observed_after_pins != expected_tip:
            detail = ("branch disappeared while image pins were rechecked"
                      if observed_after_pins is None else
                      f"branch moved from {expected_tip} to "
                      f"{observed_after_pins} while image pins were rechecked")
            _prune_failure(rep, boundary, "branch_tip_after_pins", detail)
            continue
        if b in fresh_pinned or local in fresh_pinned:
            boundary["image_pin_dependency"] = True
            _prune_failure(
                rep, boundary, "image_pin_dependency",
                "branch now carries an image pin and must be retained")
            current = set(rep.get("prune_pin_protected") or [])
            current.add(b)
            rep["prune_pin_protected"] = sorted(current)
            continue
        boundary["image_pin_dependency"] = False

        unique, cherry, why = _cherry_probe(g, main, b)
        boundary["cherry_rc"] = cherry.returncode
        if unique is None:
            _prune_failure(rep, boundary, "git_cherry", why)
            continue
        boundary["unique_commits"] = unique
        if unique:
            _prune_failure(
                rep, boundary, "unique_commits",
                f"branch now carries {len(unique)} unique commit(s)")
            continue

        # The first PR measurement may be minutes old after merge/check/push.
        # Re-measure immediately before EACH irreversible remote deletion: a PR
        # opened during this round must not have its head branch deleted (which
        # closes the PR). There is no transactional GitHub API for query+delete,
        # so this is the narrowest attainable race window.
        fresh = _upstream_pull_request_evidence(g)
        fresh_protected = _branches_serving_an_open_upstream_pr(g, fresh)
        recheck = {
            "branch": b,
            "outcome": fresh.get("outcome") if isinstance(fresh, dict) else None,
            "open_count": (fresh.get("open_count")
                           if isinstance(fresh, dict) else None),
        }
        if fresh_protected is None:
            recheck["reason"] = (fresh.get("reason")
                                 if isinstance(fresh, dict)
                                 else "malformed recheck evidence")
            rep.setdefault("prune_pr_rechecks", []).append(recheck)
            rep["prune_skipped"] = (
                f"upstream PR recheck became NORECORD before deleting {b}; "
                "stopped pruning rather than risk closing a new contribution")
            rep["needs_human"] = True
            boundary["pr_outcome"] = "norecord"
            _prune_failure(
                rep, boundary, "upstream_prs",
                recheck.get("reason", "malformed recheck evidence"))
            continue
        if b in fresh_protected or local in fresh_protected:
            recheck["protected"] = True
            rep.setdefault("prune_pr_rechecks", []).append(recheck)
            current = set(rep.get("prune_protected") or [])
            current.add(local)
            rep["prune_protected"] = sorted(current)
            boundary.update({"outcome": "retained", "blocked_at": "open_pr",
                             "reason": "branch is the head of an open upstream PR"})
            rep.setdefault("prune_rechecks", []).append(boundary)
            continue
        recheck["protected"] = False
        rep.setdefault("prune_pr_rechecks", []).append(recheck)

        # Delete the REMOTE first with an exact expected-SHA lease.  This is a
        # compare-and-swap, not a force override: a branch that moves after the
        # recheck is preserved by the server.  No local ref is touched before
        # the remote confirms deletion.
        deleted = sh(
            *g, "push", "--progress",
            f"--force-with-lease=refs/heads/{local}:{expected_tip}",
            "origin", f":refs/heads/{local}", progress=True)
        if deleted.returncode == 0:
            gone.append(b)
            boundary["remote_delete"] = "complete"
        else:
            rep.setdefault("prune_push_blocked", []).append({
                "branch": b,
                "outcome": ("stalled" if
                            deleted.returncode == watchdog.RC_STALLED
                            else "natural_failure"),
                "rc": deleted.returncode,
                "detail": (_first_error_line(deleted) if
                           deleted.returncode != watchdog.RC_STALLED else
                           "no forward progress; remote deletion UNKNOWN"),
            })
            rep["needs_human"] = True
            _prune_failure(
                rep, boundary, "remote_delete_lease",
                "remote deletion did not complete under the expected-SHA lease")
            continue

        # A local branch is separate state.  Remove it only if it still equals
        # the remote tip proved above, using update-ref's old-value argument as
        # a second lease. A locally advanced branch is preserved verbatim.
        # update-ref does not protect checked-out branches, so measure ALL
        # registered worktrees immediately before using that low-level API.
        local_tip, local_read = _ref_tip(g, f"refs/heads/{local}")
        if local_tip is None:
            if local_read.returncode in (1, 128) and not local_read.stdout:
                boundary["local_delete"] = "not_present"
            else:
                boundary["local_delete"] = "preserved_unmeasurable"
                rep.setdefault("prune_local_preserved", []).append({
                    "branch": local, "reason": "local tip could not be read",
                    "rc": local_read.returncode,
                })
                rep["needs_human"] = True
        elif local_tip != expected_tip:
            boundary["local_delete"] = "preserved_changed"
            rep.setdefault("prune_local_preserved", []).append({
                "branch": local, "expected_tip": expected_tip,
                "local_tip": local_tip,
                "reason": "local branch differs from the proven remote tip",
            })
            rep["needs_human"] = True
        else:
            bindings, inventory, why = _worktree_branches(g)
            if bindings is None:
                boundary["local_delete"] = "preserved_unmeasurable"
                rep.setdefault("prune_local_preserved", []).append({
                    "branch": local, "expected_tip": expected_tip,
                    "reason": "worktree inventory unavailable: " + why,
                    "rc": inventory.returncode,
                })
                rep["needs_human"] = True
            elif (paths := bindings.get(f"refs/heads/{local}")) or (
                    paths := bindings.get("detached")):
                # An active checkout is ordinary retained local state, not a
                # failed remote prune or unfinished contribution.
                attached = f"refs/heads/{local}" in bindings
                boundary["local_delete"] = ("preserved_checked_out" if attached
                                            else "preserved_detached_worktree")
                rep.setdefault("prune_local_preserved", []).append({
                    "branch": local, "expected_tip": expected_tip,
                    "reason": ("local branch is checked out in a registered worktree"
                               if attached else "detached worktree may reserve a branch "
                               "during rebase, bisect, or sequencer operations"),
                    "worktrees": paths,
                })
            else:
                local_deleted = sh(
                    *g, "update-ref", "-d", f"refs/heads/{local}", expected_tip)
                if local_deleted.returncode == 0:
                    boundary["local_delete"] = "complete"
                else:
                    boundary["local_delete"] = "preserved_lease_failure"
                    rep.setdefault("prune_local_preserved", []).append({
                        "branch": local, "expected_tip": expected_tip,
                        "reason": _first_error_line(local_deleted),
                        "rc": local_deleted.returncode,
                    })
                    rep["needs_human"] = True
        boundary["outcome"] = "remote_deleted"
        rep.setdefault("prune_rechecks", []).append(boundary)
    rep["pruned"] = gone


def _claude_bin():
    for c in (Path.home() / ".local/bin/claude",):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    r = subprocess.run(["bash", "-lic", "command -v claude"],
                       capture_output=True, text=True)
    p = r.stdout.strip()
    return p if p else None



def _gatekeeper_command(brief):
    """Choose one explicit CLI; never replay a partially executed AI turn.

    Codex is the current host gatekeeper. Operators may explicitly select the
    legacy Claude CLI. Missing or invalid configuration leaves cases pending;
    process completion still needs the existing remote landing proof.
    """
    backend = os.environ.get("GK_AI_BACKEND", "codex")
    if backend == "codex":
        binary = shutil.which("codex")
        argv = ([binary, "exec", "--json", "--skip-git-repo-check",
                 "--dangerously-bypass-approvals-and-sandbox", brief]
                if binary else None)
    elif backend == "claude":
        binary = _claude_bin()
        argv = ([binary, "-p", brief, "--permission-mode", "bypassPermissions",
                 "--output-format", "stream-json", "--verbose"]
                if binary else None)
    else:
        return backend, None, (
            f"unsupported GK_AI_BACKEND={backend!r}; choose codex or claude "
            "— cases left pending")
    return backend, argv, (None if argv else
                          f"no {backend} binary on this host — cases left pending")


def _atomic_json(path, value):
    """Publish one complete JSON object or leave the previous object intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _handoff_brief(cases) -> str:
    """What the gatekeeper is asked to decide, with the evidence attached."""
    lines = [
        "You are this host's fork gatekeeper. The 05:30 fork-consolidation run "
        "reached the ONE part it is not allowed to decide alone.",
        "",
        "Each case below is a `git merge` that CONFLICTED, and EACH ONE "
        "STATES ITS OWN DIRECTION — read it before deciding. `our branch -> "
        "our mainline` risks abandoning OUR fix; `upstream -> our mainline` "
        "risks dropping an UPSTREAM contribution, which is the invariant the "
        "owner states first. The standing rule is that choosing between merge "
        "and cherry-pick, and resolving the conflict, needs AI judgement — a "
        "script deciding by itself is not enough. So it stopped here and "
        "handed you the evidence rather than aborting the round or guessing.",
        "",
        "FOR EACH CASE: read the actual code, decide MERGE (take the branch "
        "whole) or CHERRY-PICK (take only the commits that are still wanted), "
        "resolve the conflict on its merits, and LAND it on that fork's "
        "mainline. Then push. If a case genuinely should not land, say so and "
        "say why — that is a decision too, and it is allowed. What is not "
        "allowed is leaving a case untouched with no verdict.",
        "",
        "RULES THAT BIND YOU HERE:",
        "  * Never force-push. If the push is rejected, report the rejection.",
        "  * Never delete a branch that is the head of an OPEN upstream PR. "
        "    The prune step already guards this; do not work around it. We "
        "    have already killed our own iverilog PR this way once.",
        "  * A conflict resolved by dropping a side is a decision to abandon "
        "    that side's work — only do it deliberately, and record it. WHICH "
        "    side is at risk depends on the case's direction: `our branch ->` "
        "    puts our fix at risk, `upstream ->` puts an upstream contribution "
        "    at risk.",
        "  * Repo artifacts are ENGLISH ONLY: commit messages, branch names, "
        "    PR titles and bodies.",
        "",
        f"{len(cases)} CASE(S):",
        "",
    ]
    for c in cases:
        _dir = c.get("direction") or "our branch -> our mainline"
        _whose = "upstream's" if _dir.startswith("upstream") else "our"
        lines.append(f"── {c['fork']} : branch `{c['branch']}` "
                     f"({c['commits']} of {_whose} commits) ──")
        lines.append(f"  direction: {_dir}")
        if c.get("conflicting_files"):
            lines.append("  conflicting files: "
                         + ", ".join(c["conflicting_files"][:12]))
        for k in c.get("commits_detail", c.get("our_commits", [])):
            lines.append(f"  {k['sha']}  {k['subject'][:90]}")
            if k.get("files"):
                lines.append("      touches: " + ", ".join(k["files"][:8]))
        if c.get("merge_stderr"):
            lines.append("  git said: "
                         + c["merge_stderr"].replace("\n", " ")[:300])
        lines.append(f"  repo path: {c['path']}")
        lines.append("")
    lines.append("Report, per case: the verdict (MERGE / CHERRY-PICK / "
                 "DECLINE), what you did, and the resulting sha or the reason "
                 "nothing landed.")
    return "\n".join(lines)


def _verify_ai_case(case):
    """Prove that one handed-off source tip reached the PUBLISHED mainline.

    The AI process exiting zero is only a transport fact.  A MERGE or
    CHERRY-PICK is complete when the fork's remote mainline carries the source
    tip's commits (by ancestry or patch equivalence).  Read the remote tip
    directly rather than trusting the shared checkout's current branch: the AI
    may have used a temporary worktree, and a local commit that was never pushed
    is exactly the incomplete landing this check must retain as pending.

    A DECLINE has a repository postcondition in exactly one case, and it is
    the case that produced this note: the source tip's TREE is already on the
    published mainline (a squash merge), so "nothing to land" is a measured
    fact and is verified here by tree identity.  Any other DECLINE remains
    deliberately unverified: prose or rc=0 cannot stand in for a proof.
    """
    fork = str(case.get("fork") or "")
    branch = str(case.get("branch") or "")
    main = str(case.get("mainline") or "")
    source_tip = str(case.get("source_tip") or "")
    path = Path(str(case.get("path") or ""))
    result = {"fork": fork, "branch": branch, "mainline": main,
              "source_tip": source_tip or None, "verified": False,
              "published_tip": None, "reason": ""}
    if not (fork and branch and main and source_tip and path.is_dir()):
        result["reason"] = (
            "handoff lacks a readable repo, mainline, or captured source tip; "
            "process completion cannot prove delivery")
        return result

    remote = sh("git", "-C", str(path), "ls-remote", "origin",
                f"refs/heads/{main}", timeout=LS_REMOTE_TIMEOUT_S)
    fields = (remote.stdout or "").split()
    if remote.returncode != 0 or not fields:
        result["reason"] = (
            f"origin/{main} could not be read after the handoff: "
            f"{_first_error_line(remote)}")
        return result
    published_tip = fields[0]
    result["published_tip"] = published_tip

    # The AI normally creates the published commit in this clone/worktree, so
    # its object is locally available.  Do not hide a missing object behind an
    # unbounded fetch here: inability to verify is an honest pending state.
    present = sh("git", "-C", str(path), "cat-file", "-e",
                 f"{published_tip}^{{commit}}")
    if present.returncode != 0:
        result["reason"] = (
            f"origin/{main} is {published_tip[:12]}, but that object is not in "
            "the checkout; delivery could not be verified without changing it")
        return result

    ancestor = sh("git", "-C", str(path), "merge-base", "--is-ancestor",
                  source_tip, published_tip)
    if ancestor.returncode == 0:
        result.update(
            verified=True,
            reason="source tip is an ancestor of the published mainline",
        )
        return result

    carried_by, tree_check = _tree_carried(
        ("git", "-C", str(path)), published_tip, source_tip)
    result["tree_identity"] = tree_check
    if carried_by:
        result.update(
            verified=True,
            reason=(f"source tip's tree is published on origin/{main} at "
                    f"{carried_by[:12]} (squash-merged: every byte of the "
                    "branch is on the mainline; nothing was left to land)"),
        )
        return result
    carried = sh("git", "-C", str(path), "cherry", published_tip, source_tip)
    if carried.returncode != 0:
        result["reason"] = (
            "could not compare source content with the published mainline: "
            + _first_error_line(carried))
        return result
    outstanding = [line for line in carried.stdout.splitlines()
                   if line.lstrip().startswith("+")]
    if outstanding:
        result["reason"] = (
            f"{len(outstanding)} source commit(s) remain absent from "
            f"origin/{main}; the case is still pending")
        return result
    result.update(verified=True,
                  reason=("source content is patch-equivalent on the "
                          "published mainline"))
    return result


def _verify_ai_cases(cases):
    return [_verify_ai_case(case) for case in cases]


def _ai_stream_tail(out: str) -> str:
    """Final message from either CLI's JSON events, or a bounded fallback."""
    for line in reversed((out or "").splitlines()):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                return str(item.get("text", "")).strip()[-600:]
        if isinstance(event, dict) and event.get("type") == "result":
            value = event.get("structured_output", event.get("result", ""))
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True)
            return str(value).strip()[-600:]
    return (out or "").strip()[-600:]


def _ai_needs_attention(ai) -> bool:
    """A launched process is not a completed landing without postconditions."""
    if not ai.get("cases"):
        return False
    return bool(not ai.get("invoked")
                or ai.get("outcome") != "natural"
                or ai.get("rc") != 0
                or not ai.get("verified"))


def _ai_transport_failure(run) -> dict | None:
    """Retain provider error events as diagnostics, never as authorization.

    A natural process exit is not necessarily a completed AI turn. Do not retry
    on another provider or infer availability from arbitrary agent prose.
    """
    if run.outcome != "natural" or run.rc == 0:
        return None
    for line in reversed((run.out or "").splitlines()):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        elif event.get("type") == "error":
            message = event.get("message")
        else:
            continue
        if isinstance(message, str) and message.strip():
            kind = ("usage_limit" if "usage limit" in message.lower()
                    else "provider_error")
            return {"kind": kind, "message": message.strip()[:1000]}
    return None


def step2b_ai_decisions(report, state_dir, apply, stall_grace_s=3600):
    """Hand every merge-vs-cherry-pick conflict to the gatekeeper AI.

    This is the step the owner singled out: "Merge 或者是 Cherry-pick 的話,
    是需要用到 AI 的, 用程式直接判斷是不夠的." Before this existed the run
    flagged `needs_human` and moved on, so a conflicted branch simply sat
    there — the six steps completed while the work they exist to consolidate
    did not.
    """
    cases = []
    for fork, rep in sorted(report.items()):
        for c in rep.get("conflicted", []) or []:
            path = FORKS / fork
            branch = str(c.get("branch") or "")
            tip = out("git", "-C", str(path), "rev-parse", "--verify", branch) \
                if branch and path.is_dir() else ""
            cases.append({**c, "fork": fork, "path": str(path),
                          "mainline": rep.get("main"),
                          # Capture the input before the AI can merge, prune, or
                          # rename the branch.  Verification is against this
                          # immutable commit, never whatever the ref means later.
                          "source_tip": tip or None})

    state_dir.mkdir(parents=True, exist_ok=True)
    pending = state_dir / "ai_decisions_pending.json"
    brief_path = state_dir / "ai_decisions_brief.txt"

    if not cases:
        # THE ROUND THAT PROVES NOTHING IS PENDING MUST SAY SO.
        #
        # This used to `return` here, before the write -- so the one round that
        # establishes the queue is empty was the one round that left yesterday's
        # entries untouched. Measured 2026-08-12: the file still carried the
        # OpenROAD/src/tap conflict from 08-11, whose merge (4ede5f5b17) was by
        # then an ancestor of HEAD. Decided, resolved, pushed -- and still
        # sitting in a file named `pending`, which is the file the next round
        # hands to a decision-maker. Nothing else in the tree removes it.
        #
        # The history is not lost by this: `ai_decisions.log` is the record of
        # what was decided and stays append-only. This is the WORK QUEUE, and a
        # queue that cannot be emptied is not a queue.
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        pending.write_text("[]\n")
        brief_path.write_text(
            "No merge-vs-cherry-pick case is pending.\n\n"
            f"The round of {stamp} walked every fork and found no conflicted "
            "branch. Any case named here previously has been resolved -- see "
            "ai_decisions.log for what was decided.\n")
        return {"cases": 0, "invoked": False, "cleared": True,
                "verified": True}

    pending.write_text(json.dumps(cases, indent=2) + "\n")
    brief = _handoff_brief(cases)
    brief_path.write_text(brief)

    if not apply:
        return {"cases": len(cases), "invoked": False,
                "verified": False,
                "note": "dry run — brief written, gatekeeper not invoked"}

    backend, command, launch_error = _gatekeeper_command(brief)
    if launch_error:
        return {"cases": len(cases), "invoked": False,
                "verified": False, "backend": backend,
                "error": launch_error}

    log = state_dir / "ai_decisions.log"
    # BLOCKING on purpose. Detaching would end this process before the
    # gatekeeper's turn produced anything, which is the failure mode that has
    # already cost this campaign four rounds on one cell.  Runtime itself is
    # NOT a verdict. The selected CLI emits JSON lifecycle/tool events as it
    # works; each output advance or process-tree CPU advance renews the lease.
    # There is no total-runtime ceiling, so a healthy long decision completes.
    run = watchdog.run_supervised(
        command,
        stall_grace_s=stall_grace_s,
        output_progress=True,
        process_tree_cpu_progress=True,
    )
    verification = _verify_ai_cases(cases)
    by_key = {(v["fork"], v["branch"]): v for v in verification}
    remaining = []
    for case in cases:
        proof = by_key.get((case.get("fork"), case.get("branch"))) or {}
        if not proof.get("verified"):
            remaining.append({**case, "verification": proof.get("reason")})

    # The pending file describes NOW, not the queue before the gatekeeper ran.
    # Successfully-delivered cases leave it; stalled/unverified cases survive.
    pending.write_text(json.dumps(remaining, indent=2) + "\n")
    if remaining:
        brief_path.write_text(
            _handoff_brief(remaining) + "\n\nPrevious automated attempt left "
            f"{len(remaining)} case(s) without a verified published "
            "postcondition. They remain pending.\n")
    else:
        brief_path.write_text(
            "No merge-vs-cherry-pick case is pending.\n\n"
            "Every case from this round was verified on its fork's published "
            "mainline. See ai_decisions.log for the retained turn.\n")

    log_error = None
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().astimezone().isoformat(timespec='seconds')} "
                     f"outcome={run.outcome} rc={run.rc} ===\n")
            fh.write(run.out or "")
            fh.write("\n--- stderr ---\n")
            fh.write(run.err or "")
            fh.write("\n")
    except OSError as exc:
        log_error = f"could not retain AI stream: {type(exc).__name__}: {exc}"

    result = {
        "cases": len(cases), "invoked": True, "rc": run.rc,
        "outcome": run.outcome,
        "elapsed_seconds": round(run.elapsed_s, 3),
        "stall_grace_seconds": stall_grace_s,
        "backend": backend,
        "progress_mode": f"{backend}_json_events_or_process_tree_cpu",
        "verified": bool(verification and not remaining),
        "verified_cases": sum(1 for v in verification if v.get("verified")),
        "unverified_cases": remaining,
        "verification": verification,
        "log": str(log),
        "tail": _ai_stream_tail(run.out),
    }
    transport_failure = _ai_transport_failure(run)
    if transport_failure:
        result["transport_failure"] = transport_failure
        result["error"] = (
            f"gatekeeper AI unavailable ({transport_failure['kind']}): "
            f"{transport_failure['message']}; {len(remaining)} case(s) remain pending")
    elif run.outcome == "stalled":
        result["error"] = (
            "gatekeeper AI made no observable progress for the configured "
            "stall grace; unfinished cases remain pending")
    elif run.outcome == "launch_error":
        result["error"] = (
            "gatekeeper AI could not be launched; cases remain pending")
    if log_error:
        result["log_error"] = log_error
    return result


def resume_ours_after_ai_landing(report, ai, apply):
    """Do the consolidation an upstream conflict DEFERRED, once the AI landed it.

    THE ROUND USED TO STOP AT A LABEL THAT DESCRIBED HOW IT GOT SOMEWHERE, NOT
    WHETHER IT ARRIVED. Measured 2026-08-20 on OpenROAD: step 1 hit a `src/sta`
    conflict taking 12 upstream commits, aborted the merge, and handed the case
    to step 2b — which resolved it, pushed it, and had the landing VERIFIED
    (`source tip is an ancestor of the published mainline`, published tip
    e739571b2618). Every upstream commit was in. Nothing was outstanding.

    And the round still refused, because two fields were written before the AI
    ran and nobody ever revisited them: `upstream_merge_outcome` stayed frozen
    at "conflict", and `ours_skipped` — set as a SEQUENCING precaution, whose own
    text says consolidation "waits for the required upstream-first base" — was
    never told the base had arrived. `check_ai_release_authorization` read both,
    correctly concluded that branch conflict discovery was incomplete, and
    returned NORECORD. That blocked run_tick, which blocked build_page, and
    https://vibeic.ai/eda-forks.html silently served three-day-old numbers until
    a person noticed. A merge that succeeded was reported as a failure.

    The fix is NOT to teach the gate that "conflict" is fine — the gate was
    right: the ours-side discovery genuinely had not run, and certifying an
    unmeasured thing as complete is a worse defect than the one being closed.
    The fix is to DO THE DEFERRED WORK. This runs after step 2b, and only for a
    fork whose deferral was exactly this sequencing one and whose upstream case
    the AI verification proves landed. Then `step2_ours` runs for real, and the
    round says `conflict_resolved` only because the measurement was taken.

    Fails closed everywhere: an unverified case, a mainline that will not
    fast-forward to the published tip, a `step2_ours` that cannot read the
    branch list, or any exception, all leave the deferral in place and the round
    NORECORD. Returns a summary for the round record.
    """
    summary = {"program": "resume_ours_after_ai_landing", "schema_version": 1,
               "applied": bool(apply), "resumed": [], "declined": []}
    if not apply:
        return summary

    verified = {}
    for v in ((ai or {}).get("verification") or []):
        if isinstance(v, dict) and v.get("verified") is True:
            verified.setdefault(str(v.get("fork") or ""), []).append(v)

    for fork, rep in sorted(report.items()):
        if fork.startswith("_") or not isinstance(rep, dict):
            continue
        # ONLY the sequencing deferral. `BRANCH LIST UNKNOWN` is a different
        # skip: it means the branch list was never read, and no later evidence
        # about the UPSTREAM side can supply the OURS side it never measured.
        if rep.get("ours_skipped") != OURS_DEFERRED_BY_UPSTREAM_CONFLICT:
            continue
        if rep.get("upstream_merge_outcome") != "conflict":
            continue

        # Match the verification to the case step 1 filed, by the branch it
        # recorded — not merely "this fork has some verified case". A verified
        # OURS-direction case says nothing about the upstream landing.
        upstream_refs = {str(c.get("branch") or "")
                         for c in (rep.get("conflicted") or [])
                         if isinstance(c, dict)
                         and c.get("direction") == "upstream -> our mainline"}
        landing = next((v for v in verified.get(fork, [])
                        if str(v.get("branch") or "") in upstream_refs), None)
        if landing is None:
            summary["declined"].append(
                {"fork": fork,
                 "reason": "no verified upstream-direction landing for this fork"})
            continue

        main = str(rep.get("main") or "")
        published_tip = str(landing.get("published_tip") or "")
        path = FORKS / fork
        if not (main and published_tip and path.is_dir()):
            summary["declined"].append(
                {"fork": fork,
                 "reason": "handoff lacks a mainline, published tip, or checkout"})
            continue

        g = ["git", "-C", str(path)]
        try:
            # The AI may have landed from a temporary worktree, so this clone's
            # mainline is not automatically at the published tip. Fast-forward
            # ONLY: a mainline that cannot reach it is not the line that landed,
            # and resetting onto it would discard whatever it actually holds.
            local_tip = out(*g, "rev-parse", "--verify", main)
            if local_tip != published_tip:
                ff = sh(*g, "merge", "--ff-only", published_tip)
                if ff.returncode != 0:
                    summary["declined"].append(
                        {"fork": fork, "local_tip": local_tip or None,
                         "published_tip": published_tip,
                         "reason": "mainline will not fast-forward to the "
                                   "published tip: " + _first_error_line(ff)})
                    continue

            # Clear the DEFERRAL before consolidating, so the only
            # `ours_skipped` that can be present afterwards is one `step2_ours`
            # set itself. Leaving the old marker in place made the step's own
            # success read as "could not measure" — caught by
            # test_a_verified_landing_runs_the_consolidation_it_deferred.
            rep.pop("ours_skipped", None)
            conflicts_before = len(rep.get("conflicted") or [])
            step2_ours(g, main, rep)
        except Exception as exc:                      # never take the round down
            rep.setdefault("ours_skipped", OURS_DEFERRED_BY_UPSTREAM_CONFLICT)
            summary["declined"].append(
                {"fork": fork, "reason": f"{type(exc).__name__}: {exc}"})
            continue

        # `step2_ours` sets its OWN `ours_skipped` when it cannot read the branch
        # list. That is a fresh, unmeasured state — leave it, and leave the
        # outcome at "conflict" so the gate still refuses.
        if rep.get("ours_skipped"):
            summary["declined"].append(
                {"fork": fork,
                 "reason": "step2_ours could not measure: "
                           + str(rep.get("ours_skipped"))[:120]})
            continue

        # Newly discovered conflicts have NOT been through step 2b: the AI
        # verified only the cases present before this resumption. Ordinary
        # step2_ours appends these cases for that later decision rather than
        # setting needs_human, so this after-AI call must retain its own hold.
        new_conflicts = len(rep.get("conflicted") or []) - conflicts_before
        if new_conflicts > 0:
            rep["needs_human"] = True
            rep["ours_skipped"] = (
                f"branch consolidation found {new_conflicts} new conflict(s) "
                "after the AI decision step; they are not verified")
            summary["declined"].append(
                {"fork": fork, "new_conflicts": new_conflicts,
                 "reason": "consolidation found conflicts after the AI step"})
            continue

        # THE PUSH STEP HAS ALREADY RUN FOR THIS FORK. `resume` happens after
        # step 2b, which happens after the whole fork loop — so anything
        # `step2_ours` merges just now is sitting on the LOCAL mainline with no
        # publication and no post-merge check behind it. Discovery is what this
        # resume can honestly complete; publishing is not.
        #
        # So: a resume that merged nothing (the ordinary case — every other fork
        # in the 2026-08-20 round reported `merged: []`) has genuinely finished
        # the deferred measurement and says so. A resume that DID merge leaves
        # the round refusing, because certifying a release whose consolidation is
        # unpublished is the same class of lie this whole change exists to stop.
        # It is not a permanent block: the next round's step 1 finds upstream
        # already in, runs `step2_ours` in its normal place BEFORE the push, and
        # publishes. One day, not never.
        merged_now = [m for m in (rep.get("merged") or []) if m]
        if merged_now:
            rep["ours_skipped"] = (
                "branch consolidation ran after this round's push step and "
                f"merged {len(merged_now)} branch(es); they are on the local "
                "mainline and NOT published, so this round cannot certify them. "
                "The next round consolidates them in the normal order.")
            summary["declined"].append(
                {"fork": fork, "merged_unpublished": len(merged_now),
                 "reason": "consolidation merged after the push step"})
            continue

        rep["upstream_merge_outcome"] = "conflict_resolved"
        rep["upstream_landing"] = {
            "verified": True,
            "published_tip": published_tip,
            "source_tip": landing.get("source_tip"),
            "reason": landing.get("reason"),
            "ours_resumed": True,
        }
        rep["upstream"] = (str(rep.get("upstream") or "").rstrip()
                           + " — RESOLVED and landed; branch consolidation ran")
        summary["resumed"].append({"fork": fork, "published_tip": published_tip})

    return summary


def same_content_divergence(g, main: str) -> str | None:
    """The remote's sha when it holds the SAME TREE as ours under a different
    commit, else None. vibeic-eda#61.

    `FasterCap` failed its nightly push every night from 2026-08-01 and would
    have kept failing forever:

        ! [rejected]  master -> master (non-fast-forward)

    One change, committed twice. Same subject, same date, and — the part that
    settles it — the same tree:

        local  HEAD^{tree}          76f4e291e08d
        remote origin/master^{tree} 76f4e291e08d

    differing only in author metadata, because the mirror was created by
    clone+push (the fork API returns 403) and the same patch was later applied
    in a clone with no shared history. Neither commit is an ancestor of the
    other, so `ahead 1 / behind 1` is STRUCTURAL: re-running the sync cannot fix
    it, it reproduces it.

    DERIVED FROM THE TWO TREE HASHES, so there is no list of mirrors to keep up
    to date — any fork that lands in this shape is handled. And it is the honest
    reading: with equal trees there is nothing of OURS to publish, only a
    different way of spelling the same content, so adopting the remote discards
    nothing. The caller proves that equality again immediately before resetting.
    """
    ours = out(*g, "rev-parse", f"{main}^{{tree}}")
    theirs = out(*g, "rev-parse", f"origin/{main}^{{tree}}")
    if not ours or not theirs or ours != theirs:
        return None
    a = out(*g, "rev-parse", main)
    b = out(*g, "rev-parse", f"origin/{main}")
    return b if a and b and a != b else None


def main_(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="without this, nothing is merged, pushed or deleted")
    ap.add_argument("--json", default=None)
    ap.add_argument("--skip-build", action="store_true",
                    help="stop after step 4; step 5 is daily_release.py")
    ap.add_argument("--no-ai", action="store_true",
                    help="write the conflict brief but do not invoke the "
                         "gatekeeper (the cases stay pending)")
    ap.add_argument("--ai-stall-grace", type=float, default=3600,
                    help="seconds of zero forward progress before the "
                         "gatekeeper AI is stopped as stalled; this is not a "
                         "total-runtime limit (default 3600)")
    ap.add_argument("--round-at", default=None,
                    help="05:30 wrapper boundary carried into current-round evidence")
    args = ap.parse_args(argv)

    if not FORKS.is_dir():
        print("daily_0530: no forks root", file=sys.stderr)
        return 2

    started_at = datetime.now().astimezone()
    round_at = (args.round_at or os.environ.get("GK_CURRENT_ROUND_AT")
                or started_at.isoformat())
    run_id = secrets.token_hex(16)
    pins = image_pins()
    report = {}
    pending_upstream_conflicts = set()
    registry = watch_only_reset.load_registry(FORKS_JSON)
    fleet, fleet_errors = registered_fleet(FORKS_JSON)
    if fleet_errors:
        registry = dict(registry)
        registry["errors"] = list(registry.get("errors") or []) + fleet_errors
    reset_results = []
    seen_reset_tools = set()
    # A malformed opt-in registry must be found before any fork is changed.
    # Continuing with an empty policy map would silently turn a protected
    # watch-only mirror back into the ordinary merge/push path.
    if registry.get("errors"):
        report["_watch_only_resets"] = watch_only_reset.make_round_evidence(
            round_at=round_at, run_id=run_id, applied=args.apply,
            registry=registry, results=[])
        report["_ai_decisions"] = {
            "cases": 0, "invoked": False, "cleared": False,
            "verified": False,
            "error": ("FORKS.json (fleet or watch-only policy) is invalid; "
                      "no fork was changed"),
        }
        report["_round"] = {
            "schema_version": 1, "run_id": run_id,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().astimezone().isoformat(),
            "applied": bool(args.apply),
        }
        if args.json:
            _atomic_json(Path(args.json), report)
        round_record.write(report, gk_state.state_dir(), when=started_at)
        for error in registry["errors"]:
            print(f"daily_0530: FORKS.json NORECORD: {error}",
                  file=sys.stderr)
        return 2
    # Every registered fork gets a checkout BEFORE the walk (or a named reason
    # it could not), so the walk below covers the whole fleet.
    unprovisioned = provision_registered_checkouts(fleet, args.apply)
    for tool, why in sorted(unprovisioned.items()):
        print(f"daily_0530: registered fork {tool} has no usable checkout: "
              f"{why}", file=sys.stderr)
    for d in sorted(FORKS.iterdir()):
        if not (d / ".git").is_dir() or d.name in unprovisioned:
            continue
        policy = registry["policies"].get(d.name)
        if policy is not None:
            seen_reset_tools.add(d.name)
            g = ["git", "-C", str(d)]
            upstream_prs = _upstream_pull_request_evidence(g)
            try:
                if upstream_prs.get("outcome") != "complete":
                    reset = watch_only_reset.refusal_record(
                        policy, apply=args.apply, round_at=round_at,
                        run_id=run_id, status="norecord",
                        reason="open-upstream-PR evidence could not be measured")
                elif upstream_prs.get("open_count") != 0:
                    reset = watch_only_reset.refusal_record(
                        policy, apply=args.apply, round_at=round_at,
                        run_id=run_id, status="blocked",
                        reason=(f"{upstream_prs.get('open_count')} open upstream "
                                "PR(s); published branch rewrite refused"))
                else:
                    def branch_cas_pr_guard():
                        evidence = _upstream_pull_request_evidence(g)
                        if evidence.get("outcome") != "complete":
                            return (False, evidence,
                                    "upstream PRs became unmeasurable at the "
                                    "branch-CAS boundary")
                        count = evidence.get("open_count")
                        return (count == 0, evidence,
                                ("" if count == 0 else
                                 f"{count} upstream PR(s) are open at the "
                                 "branch-CAS boundary"))

                    reset = watch_only_reset.apply_policy(
                        d, policy, apply=args.apply, round_at=round_at,
                        run_id=run_id,
                        destructive_guard=branch_cas_pr_guard)
            except Exception as exc:  # retained NORECORD, never a silent skip
                reset = watch_only_reset.refusal_record(
                    policy, apply=args.apply, round_at=round_at,
                    run_id=run_id, status="norecord",
                    reason=f"unexpected reset failure: {type(exc).__name__}: {exc}")
            reset_results.append(reset)
            clean = reset.get("status") == "pass"
            rep = {
                "main": policy["branch"], "conflicted": [],
                "needs_human": not clean,
                "pin": pins.get(d.name.lower()),
                "watch_only_reset": reset,
                "upstream_pull_requests": upstream_prs,
                "upstream": ("watch-only mirror exactly aligned"
                             if clean else reset.get("reason")),
                "upstream_merge_outcome": (
                    "natural_success" if clean else "unmeasurable"),
                "upstream_evidence": {
                    "ref": f"upstream/{policy['branch']}",
                    "tip_before_fetch": None,
                    "tip_seen": reset.get("upstream_tip"),
                    "fetch": "ok" if clean else "unmeasurable",
                    "confirmed_by": "ls_remote" if clean else None,
                    "remote_tip": reset.get("upstream_tip_after"),
                    "behind": 0 if clean else None,
                },
                "push": "ok" if clean else f"WATCH RESET {reset.get('status')}",
            }
            report[d.name] = rep
            continue
        g = ["git", "-C", str(d)]
        upstream_prs = _upstream_pull_request_evidence(g)
        pr_norecord = upstream_prs.get("outcome") != "complete"
        if out(*g, "status", "--porcelain", "-uno", "--ignore-submodules=all"):
            report[d.name] = {"error": "dirty worktree — skipped, nothing discarded",
                              "pin": pins.get(d.name.lower()),
                              "needs_human": True,
                              "upstream_pull_requests": upstream_prs}
            continue
        # Branch identity is a release fact, not a naming convention.  Refresh
        # origin before resolving the declaration so a deleted/renamed branch
        # becomes NORECORD instead of silently falling back to main/master.
        if args.apply:
            origin_ok, origin_why = _fetch(g, "origin", "--prune")
            if not origin_ok:
                report[d.name] = {
                    "error": "declared published branch could not be refreshed: "
                             + origin_why,
                    "mainline_selection": {
                        "outcome": "norecord", "stage": "origin_fetch",
                    },
                    "pin": pins.get(d.name.lower()),
                    "needs_human": True,
                    "upstream_pull_requests": upstream_prs,
                }
                continue
        main = published_mainline(g, d.name)
        if not main:
            report[d.name] = {
                "error": ("FORKS.json has no usable published branch, or the "
                          "fresh origin does not expose it"),
                "mainline_selection": {
                    "outcome": "norecord", "stage": "declared_branch",
                    "declared_branch": declared_branch(d.name),
                },
                "pin": pins.get(d.name.lower()),
                "needs_human": True,
                "upstream_pull_requests": upstream_prs,
            }
            continue
        # Step 4 owns the PR-evidence verdict for a usable checkout: it records
        # NORECORD and refuses pruning.  Other early-exit paths below already
        # carry their own actionable failure.  Keeping transport evidence here
        # does not by itself replace the step's retained decision.
        rep = {"main": main, "needs_human": False,
               "pin": pins.get(d.name.lower()),
               "upstream_pull_requests": upstream_prs}
        if args.apply:
            selected, selection = checkout_published_line(g, main)
            rep["mainline_selection"] = selection
            if not selected:
                rep["error"] = (
                    "PUBLISHED-LINE SELECTION STALLED — outcome UNKNOWN"
                    if selection.get("outcome") == "stalled" else
                    "published-line selection refused: "
                    + str(selection.get("reason") or "no diagnostic"))
                rep["checkout_outcome"] = selection.get("outcome")
                rep["needs_human"] = True
                report[d.name] = rep
                continue
            step1_upstream(g, main, rep)
            if rep.get("upstream_merge_outcome") == "conflict":
                # This fresh row had no attention cause before step 1. Keep
                # its known conflict cause separately until AI + deferred
                # consolidation finish; every later stage can independently
                # set needs_human, and no later success clears those failures.
                pending_upstream_conflicts.add(d.name)
                rep["needs_human"] = False
            # A stalled/failed upstream merge is not a conflict and must not be
            # followed by more merges on a base whose state was unmeasurable.
            if rep.get("upstream_merge_outcome") in {
                    "natural_success", "not_applicable"}:
                step2_ours(g, main, rep)
            else:
                rep["ours_skipped"] = OURS_DEFERRED_BY_UPSTREAM_CONFLICT

            # POST-MERGE CHECKS (vibeic-eda#89), BEFORE the push.
            #
            # Both merges above can succeed with ZERO conflicting files and still
            # produce a tree that must not ship. Measured on one OpenROAD warning
            # three times in six days — 515->519, 519->524, 524->527 — each time
            # a logger message id upstream had grown into, each time a textually
            # clean merge, each time found afterwards by a person.
            #
            # HONEST ABOUT WHAT IS AND IS NOT UNDONE: unlike `daily_merge`, which
            # merges in a throwaway worktree, this program merges into the shared
            # checkout's own mainline. Refusing here leaves the REMOTE at its
            # previous tip — nothing is published — but the LOCAL branch keeps the
            # merge commit. That is deliberate: `reset --hard` on a shared
            # checkout to tidy up after a failed check would risk discarding work
            # this program did not create, and this repo's rule is that refusing
            # is cheap and discarding is not. The report says so, so the next
            # reader is not surprised by a local branch that is ahead.
            checks = run_post_merge_checks(d, post_merge_checks(d.name))
            failing = [c for c in checks if not c["ok"]]
            if checks:
                rep["post_merge_check"] = checks
            adopt = None if failing else same_content_divergence(g, main)
            if failing:
                rep["push"] = (
                    "BLOCKED by post-merge check: "
                    + "; ".join(f"{c['name']} rc={c['rc']}: {c['detail']}"
                                for c in failing)[:300]
                    + f" — NOT pushed; origin/{main} stays at its previous tip, "
                      f"the local {main} holds the unpublished merge")
                rep["needs_human"] = True
            # EQUAL TREES, DIFFERENT SHAS -> adopt the remote, do not push
            # (vibeic-eda#61). Checked BEFORE the push, because the push would
            # be rejected and that rejection is indistinguishable from a lost
            # race once it has happened.
            elif adopt:
                # Re-prove losslessness immediately before acting: `reset --hard`
                # on a tree that is no longer equal would discard content.
                if out(*g, "rev-parse", f"{main}^{{tree}}") == \
                        out(*g, "rev-parse", f"origin/{main}^{{tree}}"):
                    _was = out(*g, "rev-parse", main)
                    sh(*g, "update-ref", f"refs/salvage/pre-adopt-{_was[:12]}", _was)
                    reset = sh(*g, "reset", "--hard", f"origin/{main}",
                               progress=True)
                    if reset.returncode == 0:
                        rep["push"] = (f"ADOPTED origin/{main} {adopt[:12]} — same tree, "
                                       f"different commit; nothing of ours to publish")
                        rep["diverged_same_content"] = True
                        step4_prune(g, main, rep, True)
                    else:
                        rep["push"] = (
                            "ADOPTION STALLED — outcome UNKNOWN"
                            if reset.returncode == watchdog.RC_STALLED else
                            f"ADOPTION FAILED (rc={reset.returncode}): "
                            f"{_first_error_line(reset)}")
                        rep["needs_human"] = True
                else:
                    rep["push"] = ("SKIPPED — the trees stopped matching between "
                                   "the check and the reset")
                    rep["needs_human"] = True
            else:
                r = sh(*g, "push", "--progress", "origin", main,
                       progress=True)
                if r.returncode == 0:
                    rep["push"] = "ok"
                    step4_prune(g, main, rep, True)
                elif r.returncode == watchdog.RC_STALLED:
                    rep["push"] = (
                        "PUSH STALLED — no forward progress; publication is "
                        "UNKNOWN, not rejected")
                    rep["push_outcome"] = "stalled"
                    rep["needs_human"] = True
                elif "non-fast-forward" in (r.stderr or ""):
                    # A DIVERGENCE is not a lost race, and only one of the two is
                    # worth waking someone for. Rendering both as `REJECTED` with
                    # `needs_human` is how a nightly alarm that fires every single
                    # night stops being read.
                    #
                    # MEASURE IT — do not assert it (vibeic-eda#75). This branch
                    # used to emit "share no ancestor and their trees differ,
                    # retrying will not resolve it" as a FIXED STRING for every
                    # non-fast-forward, so an ordinary ahead/behind divergence was
                    # reported as unresolvable. Measured on OpenROAD after a round
                    # that said exactly that: `merge-base` returns 98251dfc, and
                    # the state is 12-ahead / 3-behind. Both halves of the claim
                    # were false, and "retrying will not resolve it" is the
                    # load-bearing half — it tells the next reader, human or cron,
                    # that there is nothing to do, which is how a fork stops being
                    # chased without anyone deciding to stop chasing it. The 10
                    # commits the same line reports as merged then sit as sync lag
                    # indefinitely, because origin is what `fork_gap_report` reads.
                    _mb = out(*g, "merge-base", main, f"origin/{main}") or ""
                    _ahead = out(*g, "rev-list", "--count",
                                 f"origin/{main}..{main}") or "?"
                    _behind = out(*g, "rev-list", "--count",
                                  f"{main}..origin/{main}") or "?"
                    if _mb:
                        rep["push"] = (
                            f"BEHIND: origin/{main} moved — {_ahead} ahead / "
                            f"{_behind} behind, common ancestor {_mb[:12]}. "
                            f"Integrate origin and push again; this IS resolvable.")
                        rep["diverged_kind"] = "behind"
                    else:
                        rep["push"] = (
                            f"DIVERGED: our {main} and origin/{main} share no "
                            f"ancestor — retrying will not resolve it")
                        rep["diverged_kind"] = "unrelated"
                    rep["merge_base"] = _mb[:40]
                    rep["ahead"] = _ahead
                    rep["behind"] = _behind
                    rep["needs_human"] = True
                else:
                    rep["push"] = f"REJECTED: {r.stderr.strip()[:120]}"
                    rep["needs_human"] = True
        else:
            step4_prune(g, main, rep, False)
        report[d.name] = rep

    # A REGISTERED FORK WITH NO ROW is how the 2026-09-27 round lost cace, eqy
    # and spicebind: the release authorizer requires one row per FORKS.json
    # entry, and "no row" reads as nothing at all until it refuses.  Every
    # registered fork the walk did not reach gets a row that names why.
    for tool in sorted(set(fleet) - set(report) - set(registry["policies"])):
        why = unprovisioned.get(tool) or f"no git checkout at {FORKS / tool}"
        report[tool] = {
            "error": f"registered in FORKS.json but not walked: {why}",
            "main": str(fleet[tool].get("branch") or ""), "conflicted": [],
            "upstream_merge_outcome": "unmeasurable",
            "needs_human": True, "pin": pins.get(tool.lower()),
        }

    for tool in sorted(set(registry["policies"]) - seen_reset_tools):
        policy = registry["policies"][tool]
        reset = watch_only_reset.refusal_record(
            policy, apply=args.apply, round_at=round_at, run_id=run_id,
            status="norecord",
            reason=f"configured watch-only checkout {FORKS / tool} is missing")
        reset_results.append(reset)
        report[tool] = {
            "main": policy["branch"], "conflicted": [],
            "upstream_merge_outcome": "unmeasurable",
            "watch_only_reset": reset, "needs_human": True,
            "error": reset["reason"], "pin": pins.get(tool.lower()),
        }

    # Step 2b — the AI half. A conflict is NOT a reason to stop the round; it
    # is the one decision this script is not allowed to make alone.
    ai = step2b_ai_decisions(report, gk_state.state_dir(), args.apply and not args.no_ai,
                             stall_grace_s=args.ai_stall_grace)
    report["_ai_decisions"] = ai
    # THE DEFERRED HALF OF THE ROUND, now that step 2b has supplied the base it
    # was waiting for. Placed here and not inside the fork loop because the AI
    # runs once, after every fork has been walked. See the function's docstring
    # for the 2026-08-20 OpenROAD round this closes.
    report["_ours_resumed"] = resume_ours_after_ai_landing(
        report, ai, args.apply and not args.no_ai)
    resumed_forks = {r["fork"] for r in report["_ours_resumed"]["resumed"]}
    for fork in pending_upstream_conflicts:
        if (fork not in resumed_forks
                or report[fork].get("upstream_merge_outcome") != "conflict_resolved"):
            report[fork]["needs_human"] = True
    report["_watch_only_resets"] = watch_only_reset.make_round_evidence(
        round_at=round_at, run_id=run_id, applied=args.apply,
        registry=registry, results=reset_results)
    # Process rc is transport evidence, not delivery evidence.  Only a natural
    # turn whose every source tip is verified on the remote mainline clears the
    # case.  A stalled, failed, or rc=0-but-unverified turn stays actionable.
    # Derive the exit from FINAL fork state, not a pre-AI latch. Resolved
    # conflict history remains in the report without becoming permanent rc1;
    # post-check/push/prune/resumption failures still block independently.
    needs_human = (_ai_needs_attention(ai)
                   or any(rep.get("needs_human", False)
                          for fork, rep in report.items()
                          if not fork.startswith("_")))

    # RETAIN THE ROUND (vibeic-eda#85). The live `--json` path is a SINGLE
    # path that every morning overwrites, so the only record of what a round
    # decided was the record of the most recent one. This keeps a dated copy
    # beside it and appends the narrow per-fork rows that answer "has this ever
    # fired" to an append-only index. Best-effort by construction: retention
    # that could take the round down would be a worse defect than the one it
    # closes, so `round_record.write` returns its errors and they are printed,
    # never raised.
    report["_round"] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "applied": bool(args.apply),
    }
    # The live sidecar is an input to two later verdicts. Publish the same
    # nonce/timestamp-bearing object the retained round receives, atomically;
    # otherwise yesterday's body can be laundered under today's collector time.
    if args.json:
        _atomic_json(Path(args.json), report)
    _ret = round_record.write(report, gk_state.state_dir(), when=started_at)
    if _ret.get("error"):
        print(f"  round record NOT retained: {_ret['error']}", file=sys.stderr)
    else:
        print(f"  round retained: {_ret['stamp']}  "
              f"{_ret.get('rows', 0)} fork row(s)"
              + (f", pruned {len(_ret['pruned'])} expired"
                 if _ret.get("pruned") else ""))

    for name, r in sorted(report.items()):
        # `_`-prefixed keys are ROUND-level, not forks — the same convention
        # `round_record._fork_rows` reads by. This used to name `_ai_decisions`
        # alone, so `_round` (added just above) would have been walked as if it
        # were a fork clone. It prints nothing today only because none of the
        # keys below happen to be in it.
        if name.startswith("_"):
            continue
        if r.get("error"):
            print(f"  {name:<20} ⚠️ {r['error']}")
            continue
        bits = []
        if r.get("upstream"):
            bits.append(f"upstream: {r['upstream']}")
        if r.get("merged"):
            bits.append(f"merged {len(r['merged'])} branch(es)")
        held = r.get("ours_held") or []
        refused = [h for h in held if h.get("verdict") == "unknown"]
        if len(held) > len(refused):
            bits.append(f"held {len(held) - len(refused)} branch(es) for their PR")
        if refused:
            bits.append(f"❌ REFUSED {len(refused)} branch(es): PR state unknown")
        if r.get("conflicted"):
            bits.append(f"❌ {len(r['conflicted'])} CONFLICT")
        if r.get("pruned"):
            bits.append(f"pruned {len(r['pruned'])}")
        if r.get("push", "ok") != "ok":
            bits.append(r["push"])
        if bits:
            print(f"  {name:<20} " + "; ".join(bits))

    _ai = report.get("_ai_decisions") or {}
    if _ai.get("cases"):
        if _ai.get("invoked"):
            print(f"\n  step 2b  {_ai['cases']} merge/cherry-pick conflict(s) "
                  f"handed to the gatekeeper AI "
                  f"(outcome={_ai.get('outcome')}, rc={_ai.get('rc')}; "
                  f"{_ai.get('verified_cases', 0)} verified, "
                  f"{len(_ai.get('unverified_cases') or [])} pending) "
                  f"— log: {_ai.get('log')}")
        else:
            print(f"\n  step 2b  {_ai['cases']} conflict(s) PENDING a gatekeeper "
                  f"decision: {_ai.get('error') or _ai.get('note')}")

    print("\n  step 5 (build + verify + publish) is daily_release.py — run it next"
          if not args.skip_build else "")
    return 1 if needs_human else 0


if __name__ == "__main__":
    sys.exit(main_())
