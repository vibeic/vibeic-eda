#!/usr/bin/env python3
"""Which of our fork commits does the image SHIP? — pin model and overlay model.

WHY THIS EXISTS (measured 2026-09-27, librelane)
================================================
Q2 ("do our commits reach the image") was answered as `pin..fork_tip` minus
upstream. That is the right question for a tool the image BUILDS from the pinned
fork tree. It is the wrong question for librelane, which the image does not build
at the pin at all: the Dockerfile keeps the base image's pip-installed LibreLane
and OVERLAYS an explicit list of fork commits onto it (`for c in <sha>...; do git
show --format= $c -- librelane/ | patch ...`), plus a few whole files installed
from the pinned tree. `LIBRELANE_REF` is moved to the fork tip by every release,
so `pin..tip` reads 0 after every release while fork commits that are on main
but NOT in the overlay list ship nothing:

    LIBRELANE_REF=3adcd40 (fork main)   pin..tip = 3 substantive
    after the next release moves it     pin..tip = 0       <- the false green
    not in the overlay list, measured   2678800 (#16), 2f32449 (#17),
                                        ea9d541 (#23), 81fb28d (#24)

So for a tool whose Dockerfile overlays commits, a fork commit is SHIPPED only
when the overlay applies its content:

    listed        its sha is in the overlay list
    patch-id      its in-scope diff has the patch-id of a listed commit
                  (the same change cherry-picked or rebased)
    squash        it is on a side branch whose combined in-scope diff has the
                  patch-id of a listed commit (PR #5 was listed as its squash
                  8ec5ecd; its two branch commits were later merged as well)
    whole file    every in-scope file it touches is installed whole from the
                  pinned tree, and it is an ancestor of the pin

A fork commit with NO diff inside the overlay's scope (tests only) has nothing
the image could ship; it is listed as out of scope, visibly, never counted.
Merges are not counted either: the overlay applies no merge, and a merge's own
content is its side branch, whose commits are classified one by one.

HELD. A commit of ours can be deliberately kept out of the image (a verified
defect, a check still running). FORKS.json declares it per tool:

    "held": [{"commit": "<sha>", "reason": "<why>",
              "tracked_by": "<owner>/<repo>#<n>"}]

A held commit is shown as HELD and is not counted ONLY while its tracker is
OPEN. A closed tracker, a tracker that cannot be read, or a declaration missing
its reason or tracker counts the commit again, and says why. The declaration
never edits what Q2 asserts: the commit stays listed, with its reason.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

SHA40 = re.compile(r"^[0-9a-f]{40}$")
_ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})\s*$", re.M)
_FROM = re.compile(r"^\s*FROM\s+\S+\s+AS\s+(\S+)\s*$", re.M | re.I)
_TRACKER = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


@dataclass
class Overlay:
    """What one RUN instruction applies from a fork stage's tree."""
    stem: str                       # ARG stem, e.g. LIBRELANE
    stage: str                      # FROM ... AS <stage>
    commits: List[str]              # the per-commit loop, in order
    scope: List[str]                # pathspecs the loop's `git show` limits to
    sha_paths: List[tuple] = field(default_factory=list)   # (sha, path)
    whole_files: List[str] = field(default_factory=list)   # repo-relative


def _logical_lines(text: str) -> List[str]:
    """Dockerfile instructions with backslash continuations joined."""
    out, cur = [], []
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            cur.append(stripped[:-1])
            continue
        cur.append(stripped)
        out.append(" ".join(cur))
        cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def parse_overlays(dockerfile: str) -> Dict[str, Overlay]:
    """Every fork whose image content is an explicit per-commit overlay.

    Keyed by ARG stem. A stage `FROM ... AS <stage>` that declares
    `ARG <STEM>_REF=<sha>` names the fork; a RUN that bind-mounts that stage and
    loops `for c in <sha> ...; do ... git show --format= $c -- <scope>` is its
    overlay. A fork with no such RUN is not an overlay (it is built at the pin)
    and gets no entry -- the pin model stays its answer.
    """
    lines = _logical_lines(dockerfile)
    stage_stem: Dict[str, str] = {}
    current_stage = None
    for ln in lines:
        m = _FROM.match(ln)
        if m:
            current_stage = m.group(1)
            continue
        m = _ARG.match(ln)
        if m and current_stage:
            stage_stem.setdefault(current_stage, m.group(1))
    out: Dict[str, Overlay] = {}
    for ln in lines:
        if not re.match(r"^\s*RUN\b", ln):
            continue
        for stage, stem in stage_stem.items():
            mm = re.search(r"--mount=type=bind,from=" + re.escape(stage)
                           + r",source=\S*?,target=(\S+?)(?:,|\s)", ln)
            if not mm:
                continue
            loop = re.search(r"for\s+(\w+)\s+in\s+((?:[0-9a-f]{40}\s*)+);\s*do",
                             ln)
            if not loop:
                continue
            var, shas = loop.group(1), loop.group(2).split()
            scope = re.findall(
                r"git\s+show\s+--format=\s+\$\{?" + re.escape(var)
                + r"\}?\s+--\s+([^\s;|>]+)", ln)
            if not scope:
                continue
            sha_paths = re.findall(
                r"git\s+show\s+--format=\s+([0-9a-f]{40})\s+--\s+([^\s;|>]+)",
                ln)
            # whole files installed from the mounted tree: relative sources only
            whole = [p for p in re.findall(
                r"install\s+(?:-\S+\s+(?:\S+\s+)?)*?([^\s/$-][^\s;]*)\s+/\S+", ln)
                if any(p.startswith(s.rstrip("/") + "/") or p == s
                       for s in scope)]
            out[stem] = Overlay(stem=stem, stage=stage, commits=shas,
                                scope=sorted(set(scope)), sha_paths=sha_paths,
                                whole_files=sorted(set(whole)))
    return out


def overlay_for(overlays: Dict[str, Overlay], tool: str) -> Optional[Overlay]:
    key = tool.upper().replace("-", "_")
    for stem, ov in overlays.items():
        if stem == key or stem.replace("_", "") == key.replace("_", ""):
            return ov
    return None


# ── git ────────────────────────────────────────────────────────────────────


def _git(clone: Path, *args: str, stdin: Optional[str] = None,
         timeout: int = 120) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(clone), *args], input=stdin,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _patch_id(clone: Path, diff: str) -> Optional[str]:
    if not diff.strip():
        return ""
    out = _git(clone, "patch-id", "--stable", stdin=diff)
    if out is None:
        return None
    return out.split()[0] if out.split() else ""


def _meta(clone: Path, sha: str) -> Optional[dict]:
    meta = _git(clone, "show", "-s", "--format=%H\x1f%p\x1f%an\x1f%ad\x1f%s",
                "--date=short", sha, timeout=30)
    if meta is None:
        return None
    full, parents, an, ad, subj = (meta.strip("\n").split("\x1f")
                                   + ["", "", "", "", ""])[:5]
    return {"sha": full[:12], "sha_full": full,
            "merge": len(parents.split()) >= 2, "parents": parents.split(),
            "author": an, "date": ad, "subject": subj}


def _public(c: dict) -> dict:
    return {k: c[k] for k in ("sha", "merge", "author", "date", "subject")}


# ── held declarations ───────────────────────────────────────────────────────


def gh_tracker_state(ref: str) -> Optional[str]:
    """'open' / 'closed' for `<owner>/<repo>#<n>` (issue or PR); None unknown."""
    m = _TRACKER.match(ref or "")
    if not m:
        return None
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{m.group(1)}/{m.group(2)}/issues/{m.group(3)}",
             "--jq", ".state"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    state = r.stdout.strip() if r.returncode == 0 else ""
    return state if state in ("open", "closed") else None


def load_held(forks_json: Optional[Path]) -> Dict[str, list]:
    """tool -> its FORKS.json `held` declarations ([] when none)."""
    if forks_json is None:
        return {}
    try:
        data = json.loads(Path(forks_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    forks = data.get("forks", data) if isinstance(data, dict) else data
    out: Dict[str, list] = {}
    for f in forks if isinstance(forks, list) else []:
        if isinstance(f, dict) and isinstance(f.get("held"), list):
            out[f.get("tool")] = f["held"]
    return out


def apply_held(clone: Path, unshipped: List[dict], held_decl: list,
               tracker_state: Callable[[str], Optional[str]]
               ) -> tuple:
    """Split `unshipped` into (still counted, HELD) by the declarations.

    Returns (counted, held, notes). `notes` names every declaration that did
    NOT hold a commit and why -- a closed tracker, an unreadable tracker, a
    missing reason, or a commit that is not among the unshipped ones (already
    shipped, or not ours). Nothing a declaration says is taken on trust.
    """
    by_full = {c["sha_full"]: c for c in unshipped}
    held_rows: List[dict] = []
    notes: List[str] = []
    held_full = set()
    for d in held_decl or []:
        if not isinstance(d, dict):
            notes.append(f"held declaration is not an object: {d!r}")
            continue
        commit, reason, tracker = (d.get("commit"), d.get("reason"),
                                   d.get("tracked_by"))
        label = str(commit)[:12]
        if not (isinstance(commit, str) and commit and isinstance(reason, str)
                and reason.strip() and isinstance(tracker, str)
                and _TRACKER.match(tracker)):
            notes.append(f"held {label}: needs commit, reason and tracked_by "
                         "'<owner>/<repo>#<n>' -- counted")
            continue
        full = (_git(clone, "rev-parse", "--verify", "-q",
                     f"{commit}^{{commit}}") or "").strip()
        if not full or full not in by_full:
            notes.append(f"held {label}: not an unshipped commit of ours here "
                         "(shipped, merged away, or unknown) -- nothing held")
            continue
        state = tracker_state(tracker)
        if state != "open":
            notes.append(f"held {label}: tracker {tracker} is "
                         f"{state or 'UNREADABLE'} -- counted")
            continue
        held_full.add(full)
        held_rows.append(dict(_public(by_full[full]), reason=reason,
                              tracked_by=tracker, tracker_state=state))
    counted = [c for c in unshipped if c["sha_full"] not in held_full]
    return counted, held_rows, notes


# ── classification ──────────────────────────────────────────────────────────


def overlay_unshipped(clone: Path, pin: str, tip: str, up: str,
                      ov: Overlay) -> Optional[dict]:
    """Classify every fork-owned commit on `tip` against the overlay.

    None when anything cannot be derived: a partial answer is not an answer.
    """
    owned = _git(clone, "rev-list", "--topo-order", "--reverse", tip,
                 "--not", up, timeout=120)
    if owned is None:
        return None
    metas: Dict[str, dict] = {}
    for sha in owned.split():
        m = _meta(clone, sha)
        if m is None:
            return None
        metas[m["sha_full"]] = m

    def scope_diff(sha: str) -> Optional[str]:
        return _git(clone, "show", "--format=", sha, "--", *ov.scope)

    listed = set()
    listed_pids = {}
    for sha in ov.commits:
        full = (_git(clone, "rev-parse", "--verify", "-q",
                     f"{sha}^{{commit}}") or "").strip()
        if not full:
            return None                      # a listed commit we cannot see
        listed.add(full)
        d = scope_diff(full)
        pid = None if d is None else _patch_id(clone, d)
        if pid is None:
            return None
        if pid:
            listed_pids[pid] = full[:12]
    for sha, path in ov.sha_paths:
        listed.add(sha)

    shipped: Dict[str, str] = {}
    out_of_scope: List[dict] = []
    candidates: List[dict] = []
    for full, m in metas.items():
        if m["merge"]:
            continue
        if full in listed:
            shipped[full] = "listed"
            continue
        d = scope_diff(full)
        if d is None:
            return None
        if not d.strip():
            out_of_scope.append(_public(m))
            continue
        pid = _patch_id(clone, d)
        if pid is None:
            return None
        if pid in listed_pids:
            shipped[full] = f"patch-id of {listed_pids[pid]}"
            continue
        files = _git(clone, "show", "--format=", "--name-only", full, "--",
                     *ov.scope)
        if files is None:
            return None
        touched = {f for f in files.split() if f}
        if (touched and ov.whole_files
                and touched <= set(ov.whole_files)
                and _git(clone, "merge-base", "--is-ancestor", full, pin)
                is not None):
            shipped[full] = "whole file at the pin"
            continue
        candidates.append(m)

    # squash: a merged side branch whose combined in-scope diff is listed
    for m in metas.values():
        if not m["merge"] or len(m["parents"]) < 2:
            continue
        p1, p2 = m["parents"][0], m["parents"][1]
        side = _git(clone, "rev-list", "--reverse", "--no-merges", p2,
                    "--not", p1, up)
        if side is None:
            return None
        side = side.split()
        if not side or not all(s in metas for s in side):
            continue
        base = metas[side[0]]["parents"]
        if not base:
            continue
        d = _git(clone, "diff", base[0], p2, "--", *ov.scope)
        if d is None:
            return None
        pid = _patch_id(clone, d)
        if pid and pid in listed_pids:
            for s in side:
                shipped.setdefault(s, f"squash listed as {listed_pids[pid]}")

    unshipped = [m for m in candidates if m["sha_full"] not in shipped]
    return {
        "model": "overlay",
        "listed": len(ov.commits),
        "shipped": len(shipped),
        "shipped_how": {k[:12]: v for k, v in sorted(shipped.items())},
        "out_of_scope": out_of_scope,
        "unshipped": unshipped,
        "merges_past_pin": 0,
    }


def pin_unshipped(clone: Path, pin: str, tip: str, up: str) -> Optional[dict]:
    """The pin model: `pin..tip` minus upstream (moved here unchanged from
    fork_gap_report.ours_past_the_pin, so both producers run one derivation).

    Our commits that the image does NOT ship: `pin..<published tip>` minus
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
    subs, merges = [], 0
    for sha in out.split():
        m = _meta(clone, sha)
        if m is None:
            return None
        if m["merge"]:
            merges += 1
        else:
            subs.append(m)
    return {"model": "pin", "unshipped": subs, "merges_past_pin": merges,
            "out_of_scope": []}


def classify(clone: Path, pin: str, tip: str, up: str,
             ov: Optional[Overlay], held_decl: list,
             tracker_state: Callable[[str], Optional[str]] = gh_tracker_state
             ) -> Optional[dict]:
    """The one answer both producers publish (fork_gap_report, discover_forks).

    Keys: model, ours_unshipped, ours_unshipped_substantive, unshipped_commits,
    held_commits, held_notes, out_of_scope, and for the overlay model listed /
    shipped / shipped_how. None when it cannot be derived.
    """
    res = (overlay_unshipped(clone, pin, tip, up, ov) if ov is not None
           else pin_unshipped(clone, pin, tip, up))
    if res is None:
        return None
    counted, held, notes = apply_held(clone, res["unshipped"], held_decl,
                                      tracker_state)
    res["unshipped_commits"] = [_public(c) for c in counted]
    res["held_commits"] = held
    res["held_notes"] = notes
    res["ours_unshipped_substantive"] = len(counted)
    res["ours_unshipped"] = len(counted) + res["merges_past_pin"]
    del res["unshipped"]
    return res
