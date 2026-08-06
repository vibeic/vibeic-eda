#!/usr/bin/env python3
"""fork_gap_report — the two questions that must be answerable every day.

    Q1  how far behind upstream is the SHIPPED IMAGE?
    Q2  do our own commits and bug fixes actually REACH that image?

Both must be able to read ZERO, and a zero must mean the thing it says.

WHY THIS IS A PROGRAM
=====================
These were measured by hand and the hand got them wrong twice in one evening:

1. A missing pin was read as a zero gap. The first cut took the pin from the
   ledger's `ref` field and, when that was absent, fell back to the clone's own
   HEAD — which makes `pin..HEAD` identically 0. All six lagging tools then
   reported "purely a sync problem" and the 31-commit RELEASE lag vanished. An
   absent measurement rendered as the reassuring answer, which is the defect this
   whole campaign exists to remove.

2. "Behind" was read as one number when it is two. `behind_commits` measures
   PIN -> UPSTREAM, and that distance has two independent causes that need
   opposite fixes:

       SYNC LAG      our fork is behind upstream        -> merge upstream in
       RELEASE LAG   the image's pin is behind OUR fork -> bump the pin, rebuild

   Measured 2026-08-02: of 47 commits behind, 20 were sync and 31 were release —
   including yosys, whose fork was perfectly in sync while the image still
   lacked 3 of our own commits. "Sync harder" would not have moved that number.

RULES THIS ENCODES
==================
- The pin comes from the Dockerfile's own `ARG <TOOL>_REF`, because that is what
  the image is BUILT FROM. Any other source describes something else.
- A pin that cannot be found is `null`, never 0, and makes the run exit 2.
- A clone that cannot be read is `null`, never 0.
- The tip every count is taken FROM must be CURRENT, not merely resolvable. A
  ledger-recorded branch that has stopped tracking the default is rejected and
  said so — see `published_tip`, and vibeic-eda#92 for the run it would have
  reported green while our commits sat unshipped.
- `integrated=false` (the image does not build from our fork at all) is reported
  as its own state, because a fork that ships nothing has no meaningful pin gap
  and must not be silently counted as "0 behind".
- A CONTENTS ASSERTION is not a pin and gets its own state too (vibeic-eda#79).
  `ARG OPEN_PDKS_VOLUME_CONTENTS_SHA` records which upstream commit a PREBUILT
  ciel volume carries; nothing fetches at it. THIS REPORT IS WHERE THE MISTAKE
  CAME FROM: it read `open_pdks 18 commits image-behind-upstream`, both #74 and
  #78 cited that line as the reason to advance the ARG, and the build guard
  refused both — because advancing it rebuilds nothing and makes a true
  statement false. Such a row is reported as a fact, never as a gap, and never
  as NOT MEASURED either: "not measured" is a question still open, and this one
  is answered.

EXIT
    0  both headline numbers are zero
    1  a gap exists (the report says which kind, per tool)
    2  something could not be measured — NOT the same as zero
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
# IMPORTED, never re-derived — see its docstring, and vibeic-eda#29 for what two
# copies of one rule cost.
import pin_kinds  # noqa: E402 — build INPUT vs claim ABOUT one (vibeic-eda#79)
# IMPORTED for `_ls_remote_head`, never re-spelled: three programs now need to
# ask a remote what a branch really points at, and a fourth copy of that rule is
# how this same defect reached three files. See `fetch_confirms_current`.
import discover_forks  # noqa: E402

ARG_RE = re.compile(r"ARG\s+([A-Z0-9_]*?)_REF\s*=\s*([0-9a-f]{7,40})")


#: A command that never answered — killed on the clock, or unable to launch.
#: DISTINCT from any exit code the tool itself chose, exactly as `daily_0530`
#: defines it, so "we never got an answer" cannot be read as "git said no".
RC_NO_ANSWER = 124
#: A fetch pulls objects and legitimately runs long. `daily_0530` and
#: `daily_merge` both bound theirs at 1800; matched deliberately, so the three
#: programs that fetch the same clones cannot disagree about how long is too long.
FETCH_TIMEOUT_S = 1800


def _run(repo: Path, *args: str, timeout: int = 60):
    """The CompletedProcess, so a caller that needs the EXIT STATUS can have it.

    `_git` below keeps its old signature and its old meaning; this exists because
    the fetch guard needs to tell "git answered non-zero" from "git never
    answered", and a function that returns `Optional[str]` has already thrown
    that distinction away.
    """
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, RC_NO_ANSWER, "",
                                           f"no answer: timed out after {timeout}s")
    except (OSError, subprocess.SubprocessError) as exc:      # noqa: BLE001
        return subprocess.CompletedProcess(args, RC_NO_ANSWER, "",
                                           f"no answer: {type(exc).__name__}: {exc}")


def _first_error_line(cp) -> str:
    """The line of a failed command's output that NAMES the cause.

    git puts the diagnosis first and the boilerplate last, so `stderr[-1]` on an
    unreachable remote is `and the repository exists.` — a fragment identifying
    nothing. Same reasoning, and the same choice, as `daily_0530._first_error_line`.
    """
    for ln in ((cp.stderr or "") + "\n" + (cp.stdout or "")).splitlines():
        ln = ln.strip()
        if ln:
            return ln[:160]
    return "no error text"


def _git(repo: Path, *args: str, timeout: int = 60) -> Optional[str]:
    r = _run(repo, *args, timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else None


def pins_from_dockerfiles(repo: Path, ref: str = "origin/main") -> Dict[str, str]:
    """Every `ARG <X>_REF=<sha>` the image build declares, keyed by the ARG stem.

    Read from the committed tree, not the working copy: the question is what the
    image ships, and an uncommitted edit ships nothing.
    """
    out: Dict[str, str] = {}
    listing = _git(repo, "ls-tree", "-r", "--name-only", ref) or ""
    for path in listing.splitlines():
        if not path.endswith("Dockerfile") and not path.endswith(".hcl"):
            continue
        body = _git(repo, "show", f"{ref}:{path}")
        if not body:
            continue
        for stem, sha in ARG_RE.findall(body):
            out.setdefault(stem, sha)
    return out


def assertions_from_dockerfiles(repo: Path, ref: str = "origin/main") -> Dict[str, str]:
    """Every enforced `ARG <X>_VOLUME_CONTENTS_SHA=<sha>`, keyed by the ARG stem.

    Same tree and same reason as `pins_from_dockerfiles` — and a SEPARATE dict,
    because the two answer different questions and merging them is precisely the
    conflation vibeic-eda#79 is about. Classification is `pin_kinds`', not this
    module's: an assertion-named ARG that a fetch step reads is a misnamed PIN
    and comes back as one, so the name can never be used to escape the sweep.
    """
    out: Dict[str, str] = {}
    listing = _git(repo, "ls-tree", "-r", "--name-only", ref) or ""
    for path in listing.splitlines():
        if not path.endswith("Dockerfile") and not path.endswith(".hcl"):
            continue
        body = _git(repo, "show", f"{ref}:{path}")
        if not body:
            continue
        for stem, sha in pin_kinds.contents_assertions(body).items():
            out.setdefault(stem, sha)
    return out


def fetch_confirms_current(clone: Path, up_ref: str, do_fetch: bool = True):
    """Is `up_ref` CONFIRMED to be the remote's current tip? Returns `(ok, why)`.

    `ok=False` means the tracking state is UNKNOWN — which is neither "current"
    nor "behind", and must not be rendered as either.

    THE THIRD SITE OF ONE DEFECT. This program's fetch discarded its result
    entirely::

        if fetch:
            _git(clone, "fetch", "-q", "--all", timeout=180)   # result dropped

    Every number below it — `sync_lag`, `release_lag`, `image_behind` — is a
    `rev-list` against `upstream/<branch>`, a REMOTE-TRACKING REF that is only
    meaningful if the fetch refreshed it. A fetch that did not left this program
    counting against whatever the clone last managed to fetch, and a stale ref
    counts FEWER commits behind than there are. Small numbers read as health, and
    this program's numbers are what the published page prints.

    That it happened here is the part worth recording: `fork_gap_report` is the
    AUDITOR. `daily_0530` had the same defect in two places (fixed in 2b33719),
    `discover_forks._local_compare` never had it, and the program whose job is to
    check the other two was the last to check itself.

    THE GUARD IS ON THE CLASS, NOT ON THE STORY, for the reason 2b33719 measured
    and this program does not get to re-litigate: reading the exit status is
    NECESSARY BUT NOT SUFFICIENT. It covers only the failures git chooses to
    report as failures. A fetch can exit 0 and refresh nothing. So:

      1. read the status — a failed fetch is UNKNOWN, full stop;
      2. snapshot the ref before and compare after — if it MOVED, that is proof
         the fetch reached the remote and no further question is needed;
      3. when it moved NOTHING, ask the remote directly. That is the one case
         where "the remote had nothing new" and "the fetch refreshed nothing"
         are locally indistinguishable, so nothing local can separate them.

    ASKING THE REMOTE IS `discover_forks._ls_remote_head`, IMPORTED. It is the
    function `_local_compare` already uses for this exact question, and there are
    now three programs that need the answer. A fourth spelling of one rule is how
    two programs came to say opposite things about the same four pins (#29) — and
    it is how this defect reached three files in the first place.

    COST: one single-ref `ls-remote` per fork whose fetch moved nothing, ~0.3-1 s,
    skipped entirely whenever the ref moved.
    """
    # `--no-fetch` MEANS NO NETWORK, AND THAT INCLUDES THIS CHECK.
    #
    # The defect is on the FETCH path: a fetch ran, did not do its job, and its
    # result was thrown away. `--no-fetch` does not take that path — it is the
    # documented way to ask about the clones EXACTLY AS THEY STAND, and a guard
    # that reached for the network there would make an offline mode need a
    # network and surprise every caller of a flag whose name promises otherwise.
    #
    # MEASURED while writing this, and the reason this is a deliberate scoping
    # rather than an oversight: confirming in `--no-fetch` mode flags the real
    # yosys clone STALE (its `upstream/main` is 468ba27d91ae; the remote reports
    # main at d5f179524913) and takes the whole run to rc=2. That staleness is
    # REAL and worth someone's attention — but it is a fact about a shared clone
    # nobody refreshed, not about a fetch that misreported, and turning the
    # offline mode red for it would only teach people to stop using the flag.
    # In the round that publishes numbers, `run_0530.sh` fetches, so a stale ref
    # there still reaches the confirmation below.
    if not do_fetch:
        return True, ""

    branch = up_ref.split("/", 1)[1] if "/" in up_ref else up_ref
    before = _git(clone, "rev-parse", "-q", "--verify", up_ref)

    fr = _run(clone, "fetch", "-q", "--all", timeout=FETCH_TIMEOUT_S)
    if fr.returncode != 0:
        what = ("no answer from" if fr.returncode == RC_NO_ANSWER
                else f"FETCH FAILED (rc={fr.returncode}) —")
        return False, (f"{what} upstream: tracking state is UNKNOWN, not "
                       f"current: {_first_error_line(fr)}")

    after = _git(clone, "rev-parse", "-q", "--verify", up_ref)
    if after is None:
        return False, f"{up_ref} does not resolve after fetch — NOT MEASURED"
    if before is not None and after != before:
        return True, ""          # it moved: the fetch reached the remote

    url = _git(clone, "remote", "get-url", "upstream")
    if not url:
        return False, ("the fetch moved nothing and this clone has no `upstream` "
                       "remote to ask — NOT MEASURED, not zero")
    live = discover_forks._ls_remote_head(url, branch)
    if live is None:
        return False, (f"UNVERIFIED — the fetch moved nothing and {url} could not "
                       f"be asked whether {branch} is still {after[:12]}; a stale "
                       f"ref counts FEWER commits behind than there are")
    if live != after:
        return False, (f"STALE — {up_ref} is {after[:12]} but the remote reports "
                       f"{branch} at {live[:12]}; the fetch did not refresh it")
    return True, ""


def count(repo: Path, a: str, b: str) -> Optional[int]:
    """commits in b that are not in a. None when it cannot be answered."""
    v = _git(repo, "rev-list", "--count", f"{a}..{b}")
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def upstream_head(clone: Path) -> Optional[str]:
    for cand in ("upstream/master", "upstream/main"):
        if _git(clone, "rev-parse", "--verify", "-q", cand):
            return cand
    br = _git(clone, "symbolic-ref", "--short", "HEAD")
    if br and _git(clone, "rev-parse", "--verify", "-q", f"upstream/{br}"):
        return f"upstream/{br}"
    return None


def ours_past_the_pin(clone: Path, pin: str, up: str,
                      tip: str) -> Optional[List[dict]]:
    """Our commits that the image does NOT ship: `pin..<published tip>` minus
    what upstream has.

    Q2 was first answered with the ledger's `integrated` flag — "does the image
    build from our fork at all". It does, for OpenROAD and iverilog and three
    more, and the answer published was "0 stranded" while a fix of ours from that
    same morning sat past the pin, unbuilt. `integrated` is a fact about the
    Dockerfile; where the PIN STOPPED is a different fact, and it is the one the
    question asks about.

    DERIVED, not author-matched. Our commits are by definition the ones upstream
    does not carry, so a set difference finds them — including a commit an
    outside contributor landed on our fork, which an @vibeic/@defintek email
    pattern silently drops.

    `merge` is recorded per commit rather than filtered out here, because the two
    populations need opposite handling: a merge of ours whose CONTENT is
    upstream's is not our fix going unshipped, and counting it would raise an
    alarm after every routine 05:30 sync. The headline counts substantive only;
    the merge count stays visible so the lag is never invisible either.

    None (never []) when it cannot be derived — an unresolvable pin is NOT zero.
    """
    out = _git(clone, "rev-list", f"{pin}..{tip}", "--not", up, timeout=120)
    if out is None:
        return None
    rows: List[dict] = []
    for sha in out.split():
        meta = _git(clone, "show", "-s", "--format=%p\x1f%an\x1f%ad\x1f%s",
                    "--date=short", sha, timeout=30)
        if meta is None:
            return None                      # partial truth is not truth here
        parents, an, ad, subj = (meta.split("\x1f") + ["", "", "", ""])[:4]
        rows.append({"sha": sha[:12], "merge": len(parents.split()) >= 2,
                     "author": an, "date": ad, "subject": subj})
    return rows


def vendored_pin(forks_root: Path, led: dict) -> Optional[str]:
    """The effective pin of a fork that reaches the image INSIDE another fork.

    OpenSTA has no `ARG OPENSTA_REF`, because the image never clones it: OpenROAD
    carries it at `src/sta` and the build compiles `//src/sta:opensta` out of that
    tree. Its pin is therefore whatever commit OpenROAD's submodule points at, at
    OpenROAD's own pin — a real, exact answer that this program reported as NOT
    MEASURED simply because it looked in one place.

    Resolved here rather than read from the ledger's `pinned_ref_full`, so this
    program does not inherit another program's answer. The ledger's value is then
    compared against it, and a disagreement is surfaced rather than silently
    broken in favour of one side: two derivations of one fact that disagree is a
    finding.

    None when it cannot be resolved. Never a fallback to the host ref or to HEAD —
    the wrong-but-plausible pin is exactly what made every lagging tool read
    "0 behind" the first time this was measured by hand.
    """
    host, path = led.get("vendored_in"), led.get("vendored_path")
    host_ref = led.get("vendored_host_ref")
    if not (host and path and host_ref):
        return None
    row = _git(forks_root / host, "ls-tree", host_ref, path, timeout=60)
    if not row:
        return None
    parts = row.split()
    # `160000 commit <sha>\t<path>` — a gitlink. Anything else is not a submodule
    # pointer and must not be read as one.
    if len(parts) < 3 or parts[0] != "160000" or parts[1] != "commit":
        return None
    return parts[2]


#: `published_tip`'s verdict on the ledger's `vibeic_branch`. FOUR NAMES, because
#: the old code had TWO outcomes (a ref, or None) and reality has four — and
#: vibeic-eda#92 is exactly what a missing name costs: "resolves" was allowed to
#: stand in for "is current", and a stale ref answers cleanly.
TIP_CURRENT = "current"            #: MEASURED: the branch contains the default
TIP_BEHIND = "behind"              #: MEASURED: it does not — REJECTED
TIP_UNDETERMINED = "undetermined"  #: NOT MEASURED — never a synonym for current
TIP_NO_CLAIM = "no_claim"          #: the ledger records no branch; nothing to check


class TipVerdict(NamedTuple):
    """`(ref, state, why)` — the tip to measure against, and WHAT IS KNOWN about it.

    `ref` is None only when there is nothing defensible to measure against at
    all; the caller renders that as NOT MEASURED. `state` is never inferred from
    `ref` — a usable ref with an unverified provenance is a real and common
    outcome (`TIP_UNDETERMINED` with `ref` set), and collapsing it into "we got a
    ref, so we are fine" is the shape of the defect this type exists to end.
    """
    ref: Optional[str]
    state: str
    why: str


def default_ref(clone: Path) -> Optional[str]:
    """This clone's own default branch, as a remote-tracking ref. None if unknown.

    `origin/HEAD` is consulted LAST, not first, and that ordering is load-bearing.
    It is a LOCAL symbolic-ref that anyone can point anywhere (`git remote
    set-head`), so letting it displace a real `origin/master` would hand the
    currency check below a defeat switch: aim `origin/HEAD` at the stale branch
    and the branch becomes its own yardstick, trivially current. Reached only when
    neither conventional name exists — which covers a fork whose default is
    `develop` without opening that door.

    Its target is VERIFIED to resolve. A bare fork whose `master` was deleted
    leaves `origin/HEAD` pointing at a ref that is no longer there, and a name
    that does not resolve is not a default branch, it is a dangling string.
    """
    for c in ("origin/master", "origin/main"):
        if _git(clone, "rev-parse", "--verify", "-q", c):
            return c
    head = _git(clone, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head and head.startswith("refs/remotes/"):
        cand = head[len("refs/remotes/"):]
        if _git(clone, "rev-parse", "--verify", "-q", cand):
            return cand
    return None


def published_tip(clone: Path, led: dict) -> TipVerdict:
    """Our fork's PUBLISHED line — never the clone's HEAD, and never a STALE branch.

    These clones are shared. `HEAD` is whatever the last process to touch the
    directory left checked out, and that is not a fact about our fork.

    Measured 2026-08-02, both directions in one sweep, on the run that produced
    this function:

      OpenSTA   HEAD sat on `fix/max-fanout-applicability-…`, a branch another
                session had created and committed to 25 minutes earlier. Counting
                `pin..HEAD` reported that in-progress commit as "our fix is not in
                the shipped image" — work that was never claimed to be shipped and
                may never land.

      OpenROAD  HEAD sat one commit BEHIND `origin/master`, because a fix had just
                merged and this clone had not fetched. The same count would have
                MISSED a landed fix that genuinely is not in the image.

    An overcount and an undercount from one wrong reference. `origin/<branch>` is
    the tip we actually publish, so it is the only defensible answer to "have our
    commits reached the image".

    IT CLOSED THE HEAD ROUTE AND LEFT THE LEDGER ROUTE OPEN (vibeic-eda#92)
    ======================================================================
    Everything above is why this function refuses `HEAD`. It then took the
    ledger's `vibeic_branch` on the strength of `rev-parse --verify` — EXISTENCE,
    not currency — and ranked it ABOVE `origin/master`. A recorded branch that has
    stopped tracking the default still resolves perfectly well, and `sync_lag`,
    `release_lag` and `ours_past_the_pin` are then all counted from it. A stale
    tip counts FEWER of our commits past the pin than there are, so the report
    reads GREEN precisely when work is stranded. The docstring above exists to
    refuse an untrustworthy ref; the code below trusted a different one.

    THE PREDICATE, AND WHY IT RUNS THE DIRECTION IT DOES
    ===================================================
    Accept the branch only when it CONTAINS the repo default — `merge-base
    --is-ancestor <default> <branch>`. Stated as a property: *nothing that is on
    the default is missing from the tip*, which is exactly what the counts below
    need, since anything missing from the tip is silently subtracted from them.

    #92 asks for the opposite direction — "reject a vibeic_branch that is not
    ancestor-or-equal of origin/master" — and that predicate is INVERTED. Measured
    2026-08-05 on `vibeic/rcx-515-collision-with-upstream`, the very branch whose
    staleness prompted the issue::

        git merge-base --is-ancestor origin/vibeic/rcx-515… origin/master  -> rc 0
        git merge-base --is-ancestor origin/master origin/vibeic/rcx-515…  -> rc 1
        origin/vibeic/rcx-515…..origin/master  ->  18 commits

    A branch 18 commits BEHIND master is a strict ANCESTOR of master, so the
    literal rule would have ACCEPTED the motivating example — and it would REJECT
    a healthy publishing branch that is legitimately AHEAD of master, which is the
    only reason to keep a separate branch at all. It has both cases exactly the
    wrong way round. The direction here rejects the stale branch and accepts the
    ahead one. Both live non-default ledgers agree either way (`Trilinos` is
    EQUAL, `klayout` does not resolve), which is why the fleet as it stands cannot
    tell the two rules apart and a test has to.

    THREE STATES, NOT TWO
    =====================
    `TIP_CURRENT` measured and contains the default; `TIP_BEHIND` measured and
    does not, so it is REJECTED and the default is measured against INSTEAD — with
    the rejection stated, because a silent fallthrough is how this stayed
    invisible; `TIP_UNDETERMINED` when the question could not be answered at all.
    The third never reads as the first.

    UNDETERMINED IS NOT AUTOMATICALLY FATAL, and the asymmetry is deliberate:

      * a branch that RESOLVES BUT IS BEHIND would have been used, and would have
        produced confident wrong integers. That is a measurement defect: the
        caller holds the run at rc=2.
      * a branch that DOES NOT RESOLVE could never have been used by any code
        path — the old `rev-parse --verify` already dropped it — so nothing was
        ever miscounted. What was wrong is that the ledger's claim went unsaid.
        That is a documentation defect: it is NAMED on every run and does not turn
        the round red. `klayout` is in this state today (`vibeic/klayout-signoff-int`,
        derived from a Dockerfile comment, resolves neither locally nor on the
        remote), and a permanently red report is one people route around — the
        failure mode this module warns about in three other places.

    `ref` is None — NOT MEASURED — when there is no defensible tip: never a fall
    back to HEAD, and never a resolving branch whose currency cannot be checked.
    """
    dflt = default_ref(clone)
    claim = led.get("vibeic_branch")

    if not claim:
        if dflt:
            return TipVerdict(dflt, TIP_NO_CLAIM, "")
        return TipVerdict(None, TIP_UNDETERMINED,
                          "no ledger vibeic_branch and no default branch in this "
                          "clone — NOT MEASURED, not zero")

    ref = "origin/%s" % claim
    if not _git(clone, "rev-parse", "--verify", "-q", ref):
        return TipVerdict(
            dflt, TIP_UNDETERMINED,
            f"ledger records vibeic_branch={claim!r} but {ref} does not resolve in "
            f"this clone, so its currency is UNVERIFIABLE"
            + (f"; measuring against {dflt} instead" if dflt
               else " and there is no default branch either — NOT MEASURED"))

    if dflt is None:
        return TipVerdict(
            None, TIP_UNDETERMINED,
            f"{ref} resolves but this clone has no default branch (origin/master, "
            f"origin/main, origin/HEAD) to check it against — its currency is "
            f"UNMEASURABLE, and an unmeasurable tip is NOT MEASURED rather than "
            f"assumed current (vibeic-eda#92)")

    # `_run`, not `_git`: rc=1 is git's ANSWER (not an ancestor) and rc=128 or
    # RC_NO_ANSWER is git failing to answer. `_git` returns None for both, and a
    # function that cannot tell "no" from "no idea" has already thrown away the
    # distinction these three states exist to keep.
    r = _run(clone, "merge-base", "--is-ancestor", dflt, ref)
    if r.returncode == 0:
        return TipVerdict(ref, TIP_CURRENT, "")
    if r.returncode == 1:
        missing = count(clone, ref, dflt)
        extra = count(clone, dflt, ref)
        return TipVerdict(
            dflt, TIP_BEHIND,
            f"ledger records vibeic_branch={claim!r}, but {ref} does not contain "
            f"{dflt} ("
            f"{'?' if missing is None else missing} commit(s) of {dflt} missing "
            f"from it, {'?' if extra is None else extra} of its own that {dflt} "
            f"lacks) — REJECTED as our published line; measuring against {dflt}. "
            f"A stale ref resolves cleanly and every count taken from it would "
            f"have read GREEN while our commits sat unshipped (vibeic-eda#92)")
    return TipVerdict(
        dflt, TIP_UNDETERMINED,
        f"could not compare {ref} against {dflt} (git rc={r.returncode}: "
        f"{_first_error_line(r)}) — currency UNVERIFIED, not current; measuring "
        f"against {dflt}")


def analyse(repo: Path, forks_root: Path, ledger: Path, fetch: bool) -> dict:
    pins = pins_from_dockerfiles(repo)
    assertions = assertions_from_dockerfiles(repo)
    rows: List[dict] = []
    for lf in sorted(ledger.glob("*.json")):
        if lf.name == "index.json":
            continue
        try:
            led = json.loads(lf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tool = led.get("tool") or lf.stem
        clone = forks_root / tool
        row = {"tool": tool, "integrated": bool(led.get("integrated")),
               "pin_source": "ARG", "pin_disagreement": None,
               "ahead": led.get("ahead"), "pin": None, "kind": "pin",
               "asserted_contents": None,
               "sync_lag": None, "release_lag": None, "image_behind": None,
               "tip_state": None, "tip_note": None,
               "note": None}

        # the pin, by the ARG stem that matches this tool (case/dash-insensitive)
        key = tool.upper().replace("-", "_")

        # …and FIRST: is this tool pinned at all, or merely DESCRIBED?
        #
        # BEFORE THE PIN LOOKUP AND BEFORE ITS LEDGER FALLBACK, and both halves
        # of that are load-bearing. Renaming the ARG off `_REF` is enough to stop
        # `pins_from_dockerfiles` seeing it — and MEASURED, that alone changes
        # nothing here: `row["pin"]` falls through to the ledger's
        # `pinned_ref_full`, which still resolves in the clone, and the row comes
        # back reading `open_pdks 18 0 18 release` exactly as before. A rename
        # with no reader is a rename that hid the evidence and kept the bug.
        _asserted = (assertions.get(key)
                     or assertions.get(key.replace("_", ""))
                     or next((v for k, v in assertions.items()
                              if k.replace("_", "") == key.replace("_", "")),
                             None))
        if _asserted:
            row.update({
                "kind": "contents_assertion", "asserted_contents": _asserted,
                "note": (f"CONTENTS ASSERTION {_asserted[:12]} — not a pin. The "
                         f"artefact is prebuilt and the build only ASSERTS what "
                         f"it carries, so there is no ref to be behind: "
                         f"advancing it would rebuild nothing and make a true "
                         f"statement false (vibeic-eda#74, #78, #79)")})
            rows.append(row)
            continue
        row["pin"] = pins.get(key) or pins.get(key.replace("_", "")) or None
        if row["pin"] is None:
            for k, v in pins.items():
                if k.replace("_", "") == key.replace("_", ""):
                    row["pin"] = v
                    break
        # A fork can be pinned WITHOUT a hex ARG of its own, two legitimate ways.
        # Neither is "unpinned", and neither may fall through to a HEAD fallback —
        # that fallback is what made every gap read 0 the first time this was
        # measured by hand.
        #   * VENDORED: it arrives inside another fork's pinned ref as a submodule
        #     (OpenSTA lives at src/sta inside OpenROAD, pinned via OPENROAD_REF).
        #   * PINNED TO A BRANCH: `ARG X_REF=main` — a name, not a sha, so the hex
        #     pattern above cannot see it (the three ASAP7 trees).
        # The ledger derives both; take its answer only when it resolves in the
        # clone, so a stale or bogus value still lands on NOT MEASURED.
        if row["pin"] is None:
            cand = led.get("vendored_host_ref") or led.get("pinned_ref_full") or led.get("pinned_ref")
            if cand and clone.is_dir() and _git(clone, "rev-parse", "--verify", "-q", f"{cand}^{{commit}}"):
                row["pin"] = cand
                row["pin_source"] = ("vendored in " + str(led.get("vendored_in"))
                                     if led.get("vendored_in") else "branch pin")

        # A vendored fork has no ARG of its own; its pin lives one level in.
        if row["pin"] is None:
            vp = vendored_pin(forks_root, led)
            if vp:
                row["pin"] = vp
                row["pin_source"] = (
                    f"{led.get('vendored_in')}@"
                    f"{(led.get('vendored_host_ref') or '')[:12]}:{led.get('vendored_path')}")
                claimed = led.get("pinned_ref_full")
                if claimed and claimed != vp:
                    row["pin_disagreement"] = (
                        f"ledger says {claimed[:12]}, the submodule pointer says {vp[:12]}")

        if not clone.is_dir():
            row["note"] = "no clone — NOT MEASURED"
            rows.append(row); continue
        up = upstream_head(clone)
        if up is None:
            row["note"] = "no upstream remote — NOT MEASURED"
            rows.append(row); continue
        # RESOLVED BEFORE THE FETCH, so the guard can snapshot the ref it is about
        # to refresh. The fetch used to run first and its result was thrown away;
        # every count below reads `up`, so a fetch that did not do its job left
        # them counting against a stale ref and reporting a confident integer.
        ok, why = fetch_confirms_current(clone, up, do_fetch=fetch)
        if not ok:
            row["note"] = why
            rows.append(row); continue

        # The verdict is recorded on the row WHETHER OR NOT it changed the answer.
        # A rejection that only shows up as a different number is a rejection
        # nobody can audit, and #92 survived precisely because this step made no
        # statement about what it had chosen or why.
        verdict = published_tip(clone, led)
        row["tip_state"] = verdict.state
        row["tip_note"] = verdict.why or None
        if verdict.ref is None:
            row["note"] = verdict.why or "no published origin branch — NOT MEASURED"
            rows.append(row); continue
        tip = verdict.ref
        row["tip"] = tip
        row["sync_lag"] = count(clone, tip, up)
        if row["pin"]:
            row["release_lag"] = count(clone, row["pin"], tip)
            row["image_behind"] = count(clone, row["pin"], up)
            ours = ours_past_the_pin(clone, row["pin"], up, tip)
            row["ours_unshipped"] = None if ours is None else len(ours)
            row["ours_unshipped_substantive"] = (
                None if ours is None else len([c for c in ours if not c["merge"]]))
            row["unshipped_commits"] = (
                [] if ours is None else [c for c in ours if not c["merge"]])
        elif not row["integrated"]:
            row["note"] = "image does not build from our fork (no ARG pin) — see vibeic-eda#60"
        else:
            row["note"] = "PIN NOT FOUND in any Dockerfile — NOT MEASURED, not zero"
        rows.append(row)

    for r in rows:
        r.setdefault("ours_unshipped", None)
        r.setdefault("ours_unshipped_substantive", None)
        r.setdefault("unshipped_commits", [])
    measurable = [r for r in rows if r["image_behind"] is not None]
    # A contents assertion is EXCLUDED FROM BOTH, and the second exclusion is the
    # one that takes thought. Dropping it from `measurable` is obvious. Dropping
    # it from `unmeasured` is not, and getting that wrong replaces a false gap
    # with a false NOT-MEASURED that exits 2 every morning — a permanently red
    # report is one people route around, which is how `fork_reaches_flow_check`
    # lost its credibility (#17). "Not measured" means the question is open. This
    # question is closed: there is no ref for the artefact to be behind.
    asserted = [r for r in rows if r["kind"] == "contents_assertion"]
    unmeasured = [r for r in rows
                  if r["image_behind"] is None and r["integrated"]
                  and r["kind"] != "contents_assertion"]
    not_built = [r for r in rows
                 if not r["integrated"] and r["kind"] != "contents_assertion"]
    stranded = [r for r in not_built if (r.get("ahead") or 0) > 0]

    return {
        "assertions": [{"tool": r["tool"], "contents": r["asserted_contents"]}
                       for r in asserted],
        "q1_image_behind_upstream": sum(r["image_behind"] for r in measurable),
        "q1_forks_behind": len([r for r in measurable if r["image_behind"]]),
        "q1_sync_lag": sum(r["sync_lag"] or 0 for r in measurable),
        "q1_release_lag": sum(r["release_lag"] or 0 for r in measurable),
        "q1_unmeasured": [r["tool"] for r in unmeasured],
        "q2_forks_not_built_from_ours": [r["tool"] for r in not_built],
        "q2_our_commits_stranded": sum(r.get("ahead") or 0 for r in stranded),
        "q2_ours_past_the_pin": sum(r["ours_unshipped"] or 0 for r in rows),
        "q2_ours_past_the_pin_substantive":
            sum(r["ours_unshipped_substantive"] or 0 for r in rows),
        "q2_unshipped_commits": [dict(c, tool=r["tool"])
                                 for r in rows for c in r["unshipped_commits"]],
        "q2_unmeasured_ship": [r["tool"] for r in rows
                               if r["integrated"] and r["pin"]
                               and r["ours_unshipped"] is None],
        # Published as first-class report fields, not left to be inferred from a
        # note string: the page and the daily round both read this JSON, and a
        # finding that only exists inside prose is one no other program can act on.
        "tip_rejected": [{"tool": r["tool"], "why": r["tip_note"]}
                         for r in rows if r["tip_state"] == TIP_BEHIND],
        "tip_unverified": [{"tool": r["tool"], "why": r["tip_note"]}
                           for r in rows if r["tip_state"] == TIP_UNDETERMINED],
        "rows": rows,
        # `--no-fetch` deliberately skips `fetch_confirms_current`'s network check
        # (see its docstring), which means every number above can UNDER-report
        # the gap by however stale the local clones happen to be -- measured
        # 2026-08-07: it read Q1=0 while a fetching run of the same ledger read
        # 117, because Trilinos's clone had not been fetched in 8+ hours. That is
        # a fact about THIS RUN's confidence, not about the fleet, and it belongs
        # on the report next to the numbers it qualifies -- not only in a
        # docstring nobody reads before trusting a summary line (vibeic-eda#109).
        "measured_with_fetch": fetch,
    }


# ── vibeic-eda#60 — the unpinned four, and the contradiction they make cheap ──
#
# `open_pdks`, `ciel`, `sv2v` and `IHP-Open-PDK` are forks the image does not
# build from: no `ARG <TOOL>_REF`, no clone, the base image's copies. All four
# carry ZERO patches today, so nothing is lost — and that is why it is easy to
# miss. Whether to wire them in or stop forking them is the owner's call
# (vibeic-eda#60 states both options and declines to pick); until it is made,
# this is a standing state and not a new finding.
#
# Recorded as a baseline that MAY ONLY SHRINK, the same shape this org uses for
# `flow_step_can_fail_check` and `checker_execution_wiring_audit`. Without it
# this report is permanently rc=1 for a reason nobody is acting on, and a report
# that is always red is one people route around — which would hide the condition
# below on the day it first becomes true.
#
# EMPTY AS OF 2026-08-04 — the debt is paid, not waived. All four now carry a
# real pin, verified in the Dockerfiles rather than taken from this report's own
# "baseline shrank" note:
#
#     ARG CIEL_REF=714d1bbb...           ARG SV2V_REF=6662fa5d...
#     ARG IHP_OPEN_PDK_REF=22f2a25f...
#
# open_pdks was the fourth and is NOT a pin — it is
# `ARG OPEN_PDKS_VOLUME_CONTENTS_SHA=b344c97e...`, a claim about the prebuilt
# ciel volume. It leaves the baseline for the same reason the other three do
# (the image's copy is determined here and the ledger can see it), by a
# different mechanism (vibeic-eda#79).
#
# The report had been telling us this and exiting 1 for it, which is the design
# working; what it could not do was update itself. Left non-empty, the shrink
# note fires on every run forever and the report is permanently red — the exact
# condition this comment warns about two paragraphs up.
#
# The MECHANISM is not deleted along with the contents. Its tests now build a
# synthetic baseline instead of reading this set, because tests that draw their
# fixture from the live register stop testing anything the moment the register
# empties — the debt being paid would silently remove the guard that catches the
# next unpinned fork.
_UNPINNED_BASELINE: frozenset = frozenset()

# ── the contradiction, which NO baseline excuses ─────────────────────────────
#
# A fork with no pin that is nonetheless AHEAD is carrying a patch that cannot
# ship. The ledger will report `ahead=1` truthfully and the row will read like
# success. Today the condition is unreachable for all four — zero divergence
# from upstream — which is exactly why guarding it now is cheap, and why it is
# separate from the baseline above: being on a known list excuses NOT BEING
# BUILT FROM. It does not excuse carrying a patch that cannot reach a user.


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=Path("/home/reyerchu/vibeic-eda"))
    ap.add_argument("--forks-root", type=Path, default=Path("/home/reyerchu/vibe-ic-forks"))
    ap.add_argument("--ledger", type=Path,
                    default=Path.home() / ".cache/eda-fork-gatekeeper/ledger")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)

    if not a.ledger.is_dir():
        print("fork_gap_report: rc=2 NOT MEASURED — no ledger"); return 2
    rep = analyse(a.repo, a.forks_root, a.ledger, not a.no_fetch)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")

    if not rep["measured_with_fetch"]:
        print("⚠️  --no-fetch: every count below is against the clones AS THEY "
              "LAST STOOD, not a live upstream check. A stale clone reads as "
              "converged. Measured 2026-08-07: this mode printed Q1=0 while a "
              "fetching run of the SAME ledger read 117 -- for a trustworthy "
              "Q1, drop --no-fetch.")
        print()
    print(f"{'TOOL':<24}{'image behind':>13}{'= sync':>8}{'+ release':>11}   state")
    for r in sorted(rep["rows"], key=lambda x: -(x["image_behind"] or 0)):
        if r["note"]:
            print(f"{r['tool']:<24}{'—':>13}{'—':>8}{'—':>11}   {r['note']}")
            continue
        if not r["image_behind"]:
            continue
        print(f"{r['tool']:<24}{r['image_behind']:>13}{r['sync_lag']:>8}"
              f"{r['release_lag']:>11}   "
              f"{'sync' if not r['release_lag'] else ('release' if not r['sync_lag'] else 'both')}")

    print()
    print(f"  Q1  image behind upstream : {rep['q1_image_behind_upstream']} "
          f"across {rep['q1_forks_behind']} fork(s)"
          f"   [sync {rep['q1_sync_lag']} · release {rep['q1_release_lag']}]")
    print(f"  Q2  forks the image does NOT build from : "
          f"{len(rep['q2_forks_not_built_from_ours'])}"
          f" ({', '.join(rep['q2_forks_not_built_from_ours']) or 'none'})")
    print(f"      our commits stranded in them        : {rep['q2_our_commits_stranded']}")
    print(f"      our commits PAST THE PIN (not shipped): "
          f"{rep['q2_ours_past_the_pin_substantive']} substantive"
          f"  (+{rep['q2_ours_past_the_pin'] - rep['q2_ours_past_the_pin_substantive']}"
          f" merge commits, content is upstream's)")
    for c in rep["q2_unshipped_commits"]:
        print(f"          {c['tool']}/{c['sha']}  {c['date']}  {c['author']}")
        print(f"              {c['subject']}")
    # Named as its own category, not merely skipped. A row that vanishes is a row
    # nobody can audit, and the fact these carry — WHICH upstream commit the
    # shipped artefact contains — is the whole reason the ARG exists.
    if rep["assertions"]:
        print(f"      CONTENTS ASSERTIONS (not pins, no gap to close): "
              + ", ".join(f"{a['tool']}={(a['contents'] or '')[:12]}"
                          for a in rep["assertions"]))
    disagreed = [r for r in rep["rows"] if r.get("pin_disagreement")]
    for r in disagreed:
        print(f"  PIN DISAGREEMENT {r['tool']}: {r['pin_disagreement']}")

    # SAID OUT LOUD, both of them. The rejection holds the run (a branch that
    # resolves-but-is-stale WOULD have been counted from, so what our published
    # line even is is now an open question); the unverified one is named on every
    # run without turning the round red — see `published_tip` for why those two
    # get different treatment rather than the same one.
    for r in rep["tip_rejected"]:
        print(f"  LEDGER BRANCH REJECTED {r['tool']}: {r['why']}")
    for r in rep["tip_unverified"]:
        print(f"  LEDGER BRANCH UNVERIFIED {r['tool']}: {r['why']}")

    unmeasured = (rep["q1_unmeasured"] + rep["q2_unmeasured_ship"]
                  + [r["tool"] for r in disagreed])
    if unmeasured:
        print(f"  NOT MEASURED (never counted as zero) : {', '.join(sorted(set(unmeasured)))}")
    if unmeasured or rep["tip_rejected"]:
        return 2
    # #60 — a fork carrying a patch it cannot ship. No baseline excuses this:
    # the baseline covers "not built from", not "patched and unshippable".
    _stranded_rows = [r for r in rep["rows"]
                      if not r["integrated"] and (r.get("ahead") or 0) > 0]
    if _stranded_rows:
        print()
        print(f"  [FAIL] {len(_stranded_rows)} fork(s) carry commits that CANNOT "
              f"SHIP — no ARG pin, so the image does not build from them:")
        for r in _stranded_rows:
            print(f"      {r['tool']}: ahead={r['ahead']} with no pin. Either "
                  f"wire it into the Dockerfile or drop the patch; a fork that "
                  f"is patched and unbuilt reports success while shipping "
                  f"nothing (vibeic-eda#60).")
        return 1

    # The four unpinned forks are a recorded, owner-pending state. NEW ones are
    # not, and a baseline that grew is a regression accommodated rather than
    # fixed — so both directions are checked.
    _unpinned = set(rep["q2_forks_not_built_from_ours"])
    _new = sorted(_unpinned - _UNPINNED_BASELINE)
    _gone = sorted(_UNPINNED_BASELINE - _unpinned)
    if _gone:
        print(f"  [NOTE] baseline shrank — now pinned or no longer forked: "
              f"{', '.join(_gone)}. Remove them from _UNPINNED_BASELINE.")
    if _new:
        print(f"  [FAIL] {len(_new)} fork(s) newly not built from ours: "
              f"{', '.join(_new)}")
        return 1

    return 0 if (rep["q1_image_behind_upstream"] == 0
                 and rep["q2_ours_past_the_pin_substantive"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
