#!/usr/bin/env python3
"""assess_release.py — the SELECTIVE-MERGE assessment engine (owner directive 2026-07-17).

Retires the blind "rebase our branch onto the whole new upstream release + auto-ship"
merge. When a fork is behind a new upstream release, this instead ENUMERATES every
upstream commit we would be pulling in and, per commit, judges:

  * category      — bugfix / feature / refactor / test / ci / docs / build / other
  * relevance     — do we need it? (does it touch code paths our fork/use exercises?)
  * risk          — low / medium / high
  * conflict      — does it touch a file our CARRIED PATCHES also touch? (needs care)
  * clean_pick    — does it cherry-pick cleanly onto our vibeic branch?
  * reproduce     — for a bugfix: a concrete plan to confirm the bug manifests in OUR
                    current version BEFORE we adopt a fix (fix authored against our code)

The output is a structured assessment (+ markdown) for a human-review vibe-ic PR. The
"CLEARLY-SAFE" subset (low-risk self-contained bugfix, relevant, no overlap with our
patches, cherry-picks clean, reachable from a command we issue, and whose judgement
REPRODUCED across independent samples) is flagged and drives what prepare_merge_pr
proposes. Everything else is a human decision. Doctrine: understand + verify + adopt
selectively; never grab-and-paste.

WHAT "CLEARLY-SAFE" ACTUALLY TRIGGERS (this docstring used to misstate it): it is NOT
inert-until-enabled. It is consumed by prepare_merge_pr, gated on GK_MERGE_PR, which
run_tick.sh — the cron entrypoint — defaults to 1 (its own comment says "ARMED"). On a
clearly-safe commit the tick cherry-picks it onto a candidate branch and OPENS a PR on
the fork. It never auto-MERGES: a human still merges. The earlier text named a
`GK_ADOPT=auto-safe` switch that "gates" this and is "off by default" — no such variable
is read anywhere in this codebase, so that was a false safety claim. Treat `clearly_safe`
as a live, PR-opening decision, and keep every input to it deterministic (see llm_judge's
temperature=0 and the assess() cache).

Design notes:
  * Deterministic parts (commit enumeration via `gh api compare`, our-patch file overlap,
    clean-cherry-pick probe) are pure/testable and never spend an LLM.
  * AI usefulness judgment goes through llm_judge — a SAFE, tool-less Anthropic Messages API
    call (no shell, no GH_TOKEN, no capability given to the model). A prompt-injected commit
    body can at worst yield a WRONG judgment (caught by the human on the review PR); it can
    never run a command or exfiltrate a secret. On by default; kill-switch GK_ASSESS_AI=0 forces
    deterministic-only. Any failure degrades to recommend=manual (never auto-adopt) — PER COMMIT,
    with the reason recorded, and the report DISCLOSES the unassessed set rather than printing a
    default into the columns a reviewer triages on.
  * Never raises out of assess(); returns a report dict with an `error` on hard failure.

    python3 assess_release.py <tool>                 # assess that tool from its ledger
    python3 assess_release.py <tool> --json          # print the raw assessment JSON
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import gk_state  # noqa: E402 — WHERE state lives and WHO may write it (vibeic/vibeic-eda#12)
# The ONE reader of `behind_releases`, imported rather than re-derived: the field is
# now int-or-null and null means "containment could not be decided", which is NOT
# zero. Every private `led.get("behind_releases") or 0` is a place that turns an
# unanswered question into a reassuring number.
from discover_forks import release_gap, release_gap_unknown  # noqa: E402

STATE = gk_state.state_dir()
LEDGER = STATE / "ledger"
FORKS_DIR = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")
# NOTE: the "cap the LLM payload" constant that used to sit here was dead — nothing in this
# module read it — and its name described the wrong side of the constraint anyway. The judge's
# request sizing now lives in llm_judge (CHUNK, derived from the OUTPUT token cap).


# ── deterministic layer (no LLM) ──────────────────────────────────────────────
def _gh(path: str):
    """gh api → parsed JSON, or {'_err': ...} on ANY failure. Never raises — a missing
    `gh`, a timeout, or empty stderr must degrade to the _err path callers handle, so
    assess() keeps its never-raises contract."""
    try:
        r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                           capture_output=True, text=True, timeout=90)
    except (subprocess.SubprocessError, OSError) as e:
        return {"_err": f"{e.__class__.__name__}"}
    if r.returncode != 0:
        return {"_err": (r.stderr.strip().splitlines() or ["gh error"])[-1][:160]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"_err": "parse error"}


def upstream_commits(upstream: str, base_ref: str, new_ref: str) -> list[dict]:
    """Commits in upstream that base_ref lacks but new_ref has: base_ref...new_ref.
    Returns [{sha, title, body, files:[...]}] oldest-first (GitHub compare order)."""
    up_owner = upstream.split("/")[0]
    cmp = _gh(f"repos/{upstream}/compare/{base_ref}...{new_ref}")
    if cmp.get("_err"):
        return [{"_err": cmp["_err"]}]
    files_by = {}
    for f in cmp.get("files", []) or []:              # aggregate diff (all commits)
        files_by[f.get("filename")] = f.get("status")
    out = []
    for c in (cmp.get("commits") or []):
        msg = ((c.get("commit") or {}).get("message") or "")
        lines = msg.splitlines()
        out.append({"sha": (c.get("sha") or "")[:12], "sha_full": c.get("sha") or "",
                    "title": lines[0][:140] if lines else "",
                    "body": "\n".join(lines[1:])[:1200].strip(),
                    "url": c.get("html_url", ""),
                    "author": (((c.get("commit") or {}).get("author") or {}).get("name") or "")})
    # per-commit files need a second call each; cap it — attach aggregate files to the set
    return out, sorted(files_by)          # (commits, aggregate_changed_files)


def our_patch_files(upstream: str, up_branch: str, our_ref: str, tool: str) -> set[str] | None:
    """Files our carried patches touch (upstream_default...our_pinned_ref). A new upstream
    commit touching any of these needs care — it may collide with our enhancement. Returns
    None (UNKNOWN, not "no overlap") on a gh error, so the conflict gate fails SAFE: an
    errored lookup must never read as "touches nothing" and let a colliding commit pass."""
    up_owner = upstream.split("/")[0]
    cmp = _gh(f"repos/vibeic/{tool}/compare/{up_owner}:{up_branch}...{our_ref}")
    if cmp.get("_err"):
        return None
    return {f.get("filename") for f in (cmp.get("files") or []) if f.get("filename")}


DECISIONS = HERE / "DECISIONS.json"


def recorded_decisions(tool: str) -> dict[str, dict]:
    """Durable per-commit gatekeeper decisions for `tool` (see DECISIONS.json).

    A selective-merge decision is DATA, not a judgment to re-derive. Without this
    the tick re-asks the LLM about the same commit every day, and because that
    judgment is a sampled text completion it can answer differently on different
    days — which is exactly how magic's cc4da9a05fde flipped between
    'human decision' and 'auto-adopt' across 7 identical runs. An entry here is
    final until a human edits the file. Missing/broken file → {} (no decisions),
    never an exception: the assessment must still run.
    """
    try:
        blob = json.loads(DECISIONS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    d = blob.get(tool)
    return d if isinstance(d, dict) else {}


def already_carried(tool: str, our_ref: str, commits: list[dict]) -> set[str]:
    """The subset of `commits` our shipped ref ALREADY contains.

    Two ways a commit can already be ours, and the second is the one that was
    being missed:

      * ANCESTRY — the sha is literally an ancestor of our pinned ref (we merged
        or rebased past it);
      * PATCH-ID — we CHERRY-PICKED it, so the change is ours under a different
        sha. `git patch-id --stable` gives the same id for both.

    Why this matters (magic, 2026-07-25): the range 8.3.674 -> 8.3.676 was
    assessed 7 days running and 2 of its 3 commits were reported as awaiting a
    human decision — but BOTH were already in what we ship. cc4da9a05fde was a
    direct ancestor, and a22b7508acfe was carried as cherry-pick fe91f011
    (patch-id c39ec7531d7a5eb7 on both). `behind_releases` compares RELEASE
    TAGS, but selective-merge adopts COMMITS, so after adopting the useful ones
    the tag never advances and the fork reads "behind" forever — re-proposing
    work that is already done.

    Returns the SHORT shas that are already carried. Empty set when we cannot
    tell (no clone / git failure): unknown must read as "not carried" so a
    genuinely new commit is never silently dropped from review.
    """
    clone = FORKS_DIR / tool
    if not commits or not our_ref or not (clone / ".git").is_dir():
        return set()

    def _git(*args: str, timeout: int = 60):
        try:
            return subprocess.run(["git", "-C", str(clone), *args],
                                  capture_output=True, text=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            return None

    def _patch_id(rev: str) -> str | None:
        show = _git("show", rev)
        if show is None or show.returncode != 0 or not show.stdout:
            return None
        try:
            pid = subprocess.run(["git", "patch-id", "--stable"], input=show.stdout,
                                 capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            return None
        out = (pid.stdout or "").split()
        return out[0] if out else None

    carried: set[str] = set()
    # Patch-ids of everything our ref carries since the fork point — computed once.
    ours: set[str] = set()
    log = _git("log", "--format=%H", f"{our_ref}", "-n", "400", timeout=120)
    if log is not None and log.returncode == 0:
        for h in (log.stdout or "").split():
            p = _patch_id(h)
            if p:
                ours.add(p)
    for c in commits:
        sha = c.get("sha_full") or c.get("sha") or ""
        if not sha:
            continue
        anc = _git("merge-base", "--is-ancestor", sha, our_ref)
        if anc is not None and anc.returncode == 0:
            carried.add(c["sha"])
            continue
        p = _patch_id(sha)
        if p and p in ours:
            carried.add(c["sha"])
    return carried


def clean_cherrypick(tool: str, our_ref: str, commit_sha: str) -> bool | None:
    """Probe (in the local fork clone, non-destructive) whether commit_sha cherry-picks
    cleanly onto our_ref. None if we can't tell (no clone / fetch fail). Never mutates
    the checked-out branch: uses a throwaway detached worktree, always cleaned up."""
    clone = FORKS_DIR / tool
    if not (clone / ".git").is_dir():
        return None
    wt = Path("/tmp") / f"gk-cp-{tool}-{commit_sha[:8]}"
    subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(wt)],
                   capture_output=True)
    subprocess.run(["rm", "-rf", str(wt)], capture_output=True)
    try:
        r = subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach",
                            str(wt), our_ref], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        # make sure the commit object is present
        subprocess.run(["git", "-C", str(wt), "fetch", "-q", "--all"], capture_output=True, timeout=180)
        cp = subprocess.run(["git", "-C", str(wt), "cherry-pick", "--no-commit", commit_sha],
                            capture_output=True, text=True, timeout=120)
        clean = cp.returncode == 0
        subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True)
        subprocess.run(["git", "-C", str(wt), "reset", "--hard"], capture_output=True)
        return clean
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(wt)],
                       capture_output=True)
        subprocess.run(["rm", "-rf", str(wt)], capture_output=True)


# ── AI classification layer (safe tool-less API judge, fail-safe) ─────────────
NOT_ASSESSED = "not-assessed"


def _not_assessed(why: str = "") -> dict:
    """A commit the classifier never reached a conclusion about.

    Every field a reviewer triages on says NOT_ASSESSED rather than carrying a default
    that reads like a measurement. This constant used to be
    `category="other", relevant=None, risk="high"`, and on magic 8.3.674 → 8.3.678
    (2026-07-28) that printed 105 rows of "high risk" — for commits whose judgments the
    model had in fact completed as `risk: low` before its reply was cut off at the
    output cap. A classifier's I-don't-know must never be rendered in the column that
    holds its verdict; `high` there is a fabricated finding, and a reviewer cannot tell
    it apart from a real one.

    `recommend` stays "manual" — that IS the correct action, and it keeps these commits
    inside `outstanding` so an unassessed range can never read as settled. `_note` is
    what stops the assessment being cached (see assess()).
    """
    why = why or "the AI judge produced no classification for this commit"
    return {"category": NOT_ASSESSED, "relevant": None, "risk": NOT_ASSESSED,
            "summary": f"NOT ASSESSED — {why}", "reproduce": "", "recommend": "manual",
            "_note": why}


def _normalize(parsed, commits: list[dict]) -> dict:
    """Map exactly the commits we asked about to their assessment; any sha the model
    omitted or returned non-dict for is reported as NOT ASSESSED (never auto-adopt)."""
    out = {}
    for c in commits:
        a = parsed.get(c["sha"]) if isinstance(parsed, dict) else None
        out[c["sha"]] = a if isinstance(a, dict) else _not_assessed(
            "the stubbed judgment omitted this commit")
    return out


def classify_commits(tool: str, role: str, commits: list[dict]) -> dict:
    """Classify commits → {sha: {category, summary, relevant, risk, reproduce, recommend}}.

    Judges each upstream commit's usefulness to our fork via llm_judge (the SAFE, tool-less
    Anthropic Messages API call — no shell, no GH_TOKEN, no capability given to the model, so
    a prompt-injected commit body can at worst produce a WRONG judgment that the human catches
    on the review PR; it can never run a command or exfiltrate a secret). Kill-switch:
    GK_ASSESS_AI=0 forces deterministic-only (every commit → NOT ASSESSED / manual).
    GK_ASSESS_STUB mocks it for tests. Any failure (no token, API error, truncated reply,
    bad shape) is reported PER COMMIT as NOT ASSESSED — never fatal, never auto-adopt on a
    failed judgment, and never all-or-nothing: the judge returns the verdicts it did
    establish alongside a reason for each one it did not, and both survive to the report.
    """
    if not commits:
        return {}
    stub = os.environ.get("GK_ASSESS_STUB")
    if stub:
        try:
            return _normalize(json.loads(Path(stub).read_text()), commits)
        except (OSError, json.JSONDecodeError):
            return {c["sha"]: _not_assessed("the stubbed judgment could not be read")
                    for c in commits}
    if os.environ.get("GK_ASSESS_AI", "1") not in ("1", "true", "yes"):
        return {c["sha"]: _not_assessed("AI judgment is switched off (GK_ASSESS_AI=0)")
                for c in commits}   # kill-switch: deterministic-only
    try:
        import llm_judge
    except Exception:  # noqa: BLE001 — a judge hiccup must never break the assessment
        return {c["sha"]: _not_assessed("the judge module could not be loaded") for c in commits}
    try:
        outcome = llm_judge.judge(tool, role, commits)
    except Exception:  # noqa: BLE001
        outcome = None
    if not isinstance(outcome, llm_judge.JudgeOutcome):
        return {c["sha"]: _not_assessed("the judge returned no usable result") for c in commits}
    verdicts, why = outcome.verdicts, outcome.unassessed
    out = {}
    for c in commits:
        v = verdicts.get(c["sha"])
        if not isinstance(v, dict):
            # PARTIAL SURVIVAL: only the shas actually missing are degraded, each with the
            # judge's own reason. One truncated reply used to discard all 80 judgments.
            out[c["sha"]] = _not_assessed(why.get(c["sha"], ""))
            continue
        useful = bool(v.get("useful"))
        # useful → adopt-candidate (category=bugfix + recommend=adopt); the DETERMINISTIC gate
        # in assess() (clean_cherrypick + no conflict + low risk) still decides auto-safe vs human.
        out[c["sha"]] = {"category": "bugfix" if useful else "other",
                         "relevant": useful,
                         "risk": v.get("risk") if v.get("risk") in ("low", "medium", "high") else "medium",
                         "summary": str(v.get("reason", ""))[:200],
                         "reproduce": "",
                         "recommend": "adopt" if useful else "skip"}
    return out


# ── combine → assessment ──────────────────────────────────────────────────────
def _passes_static_gate(cls: dict, touches_our_files: bool, clean_pick: bool | None,
                        reach: dict | None = None) -> bool:
    """Everything the auto-adopt gate asks EXCEPT whether the judgement reproduces.

    Split out so the re-sample in assess() can be spent on exactly the commits that
    already pass every other condition (vibeic/vibeic-eda#6) — one commit out of 105 on
    the range that motivated it — instead of on the range. This is NOT the gate; the
    gate is `_clearly_safe`, and nothing but assess()'s candidate pre-filter may call
    this. Passing it is a necessary condition for auto-adopt, never a sufficient one.

    `relevant` is the model's opinion. `reach` is a program's (see reachability.py).
    When they DISAGREE — the model says relevant, the surface analysis says nothing we
    issue can reach the changed symbols — the commit drops to human review with both
    statements on the row (vibeic/vibeic-eda#5). An UNKNOWN reachability result leaves
    the model's verdict standing: "I could not determine the surface" is not
    "unreachable", and demoting on it would silently gate every candidate.
    """
    if reach is not None and reach.get("verdict") == "unreachable":
        return False
    return (cls.get("category") == "bugfix"
            and cls.get("risk") == "low"
            and cls.get("relevant") is True
            and cls.get("recommend") == "adopt"
            and not touches_our_files
            and clean_pick is True)


def _clearly_safe(cls: dict, touches_our_files: bool, clean_pick: bool | None,
                  reach: dict | None = None, agreement: dict | None = None) -> bool:
    """The narrow gate for auto-adopt: an unambiguous, self-contained, relevant, low-risk
    bugfix that does NOT overlap our patches, cherry-picks cleanly, touches code
    something we run can actually reach, AND whose judgement REPRODUCED across
    independent samples. Anything less → human.

    `agreement` (vibeic/vibeic-eda#6) is llm_judge.confirm's per-sha result. ABSENT
    AGREEMENT DEMOTES — unlike `reach`, where "I could not determine the surface" is
    honestly not "unreachable" and leaves the verdict standing. Here the missing input
    IS the finding: no agreement record means the verdict rests on ONE sample, and a
    single sample is measurably a coin toss on exactly the borderline commits this
    tier decides (three re-judgements of one 105-commit range returned three different
    useful sets). A caller that forgets to confirm must get `human`, not `auto-safe`;
    fail-closed is the whole point of this gate.
    """
    if not isinstance(agreement, dict):
        return False
    # `complete` is redundant against a record llm_judge.confirm produced (it computes
    # agree = complete and all-match) and is asserted anyway, so that a hand-built or
    # future-shaped record cannot claim agreement over samples that never arrived.
    if agreement.get("agree") is not True or agreement.get("complete") is not True:
        return False
    return _passes_static_gate(cls, touches_our_files, clean_pick, reach)


def _no_agreement(cands: list[dict], why: str) -> dict:
    """Every candidate, unconfirmed, for one shared reason. NOT agreement → demoted."""
    return {c["sha"]: {"agree": False, "complete": False, "readings": [], "detail": why}
            for c in cands}


def _confirm_candidates(tool: str, role: str, cands: list[dict], cls_map: dict) -> dict:
    """Independent RE-JUDGEMENTS of only the commits that already pass every OTHER
    clearly-safe condition (vibeic/vibeic-eda#6). → {sha: agreement dict}.

    The narrowness is the design. `cands` is the output of `_passes_static_gate`, which
    on the range that motivated this issue was 1 commit of 105 — so the whole treatment
    costs `SAMPLES - 1` extra requests, not `SAMPLES` x the range.

    The extra samples go through `classify_commits`, the SAME classifier the first
    reading came from, so the stub and kill-switch paths are confirmed by the code that
    produced them rather than bypassing it. A stub is deterministic by construction: it
    agrees, and the detail line says which readings it agreed on.

    Never raises, and every failure mode lands on "not confirmed" — which demotes.
    """
    if not cands:
        return {}
    try:
        import llm_judge
    except Exception:  # noqa: BLE001
        return _no_agreement(cands, "the judge module could not be loaded, so this verdict "
                                    "rests on ONE sample — not auto-adopted")
    first = {}
    for c in cands:
        v = cls_map.get(c["sha"]) or {}
        first[c["sha"]] = (bool(v.get("relevant")),
                           v.get("risk") if v.get("risk") in ("low", "medium", "high") else "medium")

    def sampler(cs, _t=tool, _r=role):
        m = classify_commits(_t, _r, cs)
        return {sha: (None if v.get("category") == NOT_ASSESSED
                      else (bool(v.get("relevant")),
                            v.get("risk") if v.get("risk") in ("low", "medium", "high") else "medium"))
                for sha, v in m.items()}

    try:
        res = llm_judge.confirm(cands, first, sampler)
    except Exception:  # noqa: BLE001
        return _no_agreement(cands, "the confirmation round errored, so this verdict rests "
                                    "on ONE sample — not auto-adopted")
    out = {}
    for c in cands:
        a = res.get(c["sha"])
        if not isinstance(a, llm_judge.Agreement):
            out[c["sha"]] = {"agree": False, "complete": False, "readings": [],
                             "detail": "the confirmation round returned nothing for this "
                                       "commit — not auto-adopted"}
            continue
        out[c["sha"]] = {"agree": bool(a.agree), "complete": bool(a.complete),
                         "readings": [list(r) if r is not None else None for r in a.readings],
                         "detail": a.detail}
    return out


CACHE = STATE / "assessment-cache"

# ── ASSESSOR IDENTITY (vibeic/vibeic-eda#4) ──────────────────────────────────
# The files whose CONTENT decides what a judgement says. Anything listed here is
# hashed into the cache key, so editing it misses the cache BY CONSTRUCTION.
#
# This is derived, never declared. A hand-maintained `ASSESSOR_VERSION = 3` would
# have to be bumped by whoever edits the judge — and the one time they forget is
# exactly the time a stale verdict replays. Hash the bytes instead: forgetting is
# not possible.
#
# reachability.py is here for the same reason as llm_judge.py: it decides what a row
# says, so editing it must re-judge cached ranges rather than have the change masked.
ASSESSOR_SOURCES = (HERE / "llm_judge.py", HERE / "reachability.py")


def _assessor_knobs() -> dict:
    """The RUNTIME inputs-to-behaviour that are not fixed by the source bytes.

    The model id and the chunk size are env-overridable (GK_JUDGE_MODEL /
    GK_JUDGE_CHUNK / GK_JUDGE_MAX_TOKENS), so two runs of identical source can
    still be two different assessors. The system prompt IS in the source, but it
    is named separately because it is the behaviour contract — a reader looking
    for "is the prompt part of the identity?" must find a yes here, not have to
    reason about which file it lives in.

    `samples` joins them for the same reason (vibeic/vibeic-eda#6): how many
    independent judgements an auto-adopt verdict must survive decides what the
    `decision` column says, and GK_JUDGE_SAMPLES can change it without changing a
    byte of source. A verdict confirmed once and a verdict confirmed twice are two
    different claims, so they must not share a cache entry.

    Never raises: a judge that will not import is itself a distinct assessor.
    """
    try:
        import llm_judge
    except Exception:  # noqa: BLE001
        return {"model": os.environ.get("GK_JUDGE_MODEL", "?"),
                "chunk": os.environ.get("GK_JUDGE_CHUNK", "?"),
                "max_tokens": os.environ.get("GK_JUDGE_MAX_TOKENS", "?"),
                "samples": os.environ.get("GK_JUDGE_SAMPLES", "?"),
                "prompt": "judge-unimportable"}
    prompt = f"{llm_judge._SYS_IDENTITY}\n{llm_judge._SYS_TASK}"
    return {"model": llm_judge.MODEL, "chunk": llm_judge.CHUNK,
            "max_tokens": llm_judge.MAX_TOKENS, "samples": llm_judge.SAMPLES,
            "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]}


def assessor_id() -> str:
    """Content identity of the thing that DOES the assessing.

    `_cache_key` used to identify only the assessment's INPUT — the tool, the
    upstream range, the ref our carried patches sit on. Its docstring said "same
    key ⇒ nothing we assess over has changed", which is true and is not the
    premise a replay needs: replaying is sound only when nothing we assess WITH
    has changed either. f312813 repaired what the judge concludes about identical
    commits, and for every range already in the cache that repair was invisible —
    the tick printed "unchanged range — replayed from cache" and never called the
    new code. A stale verdict and a current one rendered identically.

    An unreadable source file hashes as a distinct constant rather than being
    skipped: "I could not read the judge" must not collide with any real judge.
    """
    h = hashlib.sha256()
    for p in ASSESSOR_SOURCES:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    for k, v in sorted(_assessor_knobs().items()):
        h.update(f"{k}={v}\0".encode("utf-8"))
    return h.hexdigest()[:16]


def judge_context_id(tool: str, role: str) -> str:
    """Content identity of the QUESTION the judge is asked about this fork
    (vibeic/vibeic-eda#11).

    `assessor_id` answers "who is judging"; this answers "what were they asked". They are
    different things and must be keyed separately, because they have different BLAST
    RADII. The judge is one object shared by every fork, so changing it re-judges the
    fleet — correctly. A `role` belongs to ONE fork, so putting it in `assessor_id` (or
    hashing `FORKS.json` into it) would make "OpenSTA's description was reworded", or even
    "a fork was added", re-judge magic's cached range for a question that did not move.

    Derived from `llm_judge.system_prompt` — the renderer the request itself uses — and
    NOT from `FORKS.json`, for three reasons measured on this codebase:

      * the prompt is what changes the verdict. `FORKS.json` also carries `org`,
        `upstream`, `_comment` and fourteen other forks' entries, none of which any
        prompt has ever seen;
      * the value that reaches the judge comes from the LEDGER, not from `FORKS.json`.
        vibeic/vibeic-eda#10 is the proof that those two can differ — ledgers are written
        by discovery and never pruned, so keying on the file would key on a document that
        is upstream of, and can disagree with, the actual input;
      * the render NORMALIZES. An absent role, `""`, and the literal "EDA tool" are three
        `FORKS.json` states and one question; hashing the raw string would re-judge a
        range whose prompt is byte-identical.

    Never raises, and the degraded value is DISTINCT from every real one: a judge that
    will not import, or a template that will not format, is not the same question as any
    that does. (Such a run also classifies every commit NOT ASSESSED, which the cache gate
    already refuses to store — this only has to avoid colliding.)
    """
    try:
        import llm_judge
        blob = json.dumps(llm_judge.system_prompt(tool, role), sort_keys=True,
                          ensure_ascii=False)
    except Exception:  # noqa: BLE001 — an unrenderable question is a distinct question
        blob = json.dumps(["<judge-context-unrenderable>", tool, role or ""],
                          ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_key(tool: str, base_ref: str, new_ref: str, our_ref: str | None,
               assessor: str, question: str = "") -> str:
    """Identity of an assessment: its INPUT (tool, upstream range, the ref our carried
    patches sit on), the QUESTION asked about it, and its ASSESSOR (the content hash of
    the classifier). Same key ⇒ nothing we assess OVER, nothing we ASK, and nothing we
    assess WITH has changed.

    `question` defaults to empty ONLY so the key shape can be exercised without a judge
    import; `assess()` — the sole caller — always passes `judge_context_id(...)`, and
    `test_the_key_assess_actually_writes_carries_the_question` pins that end to end
    rather than trusting this signature. An empty component is not a legal
    `judge_context_id` value (a sha256 prefix is never ""), so the two never collide.
    """
    return f"{_cache_input_prefix(tool, base_ref, new_ref, our_ref)}|{question}|{assessor}"


def _cache_input_prefix(tool: str, base_ref: str, new_ref: str, our_ref: str | None) -> str:
    """The INPUT half of the key — everything except the assessor."""
    return f"{tool}|{base_ref}|{new_ref}|{(our_ref or '')[:12]}"


def _cache_blob(tool: str) -> dict:
    try:
        blob = json.loads((CACHE / f"{tool}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _cache_get(tool: str, key: str) -> dict | None:
    rep = _cache_blob(tool).get(key)
    return rep if isinstance(rep, dict) else None


def _prior_keys(tool: str, prefix: str) -> list[tuple[str | None, str | None]]:
    """(question, assessor) this exact INPUT was previously judged under, per cache entry.

    `None` marks a component the stored key's SHAPE did not carry, and the shapes are a
    history of this cache's widenings:

        <prefix>                      pre-#4  → (None, None)   input only
        <prefix>|<assessor>           #4      → (None, assessor)
        <prefix>|<question>|<assessor> #11    → (question, assessor)

    Used only to EXPLAIN a miss. Each widening re-judges every cached range exactly once,
    and an unexplained spike in API calls is how a correct invalidation gets mistaken for
    a bug and reverted — so the explanation has to name WHICH component moved. "The
    assessor changed" printed over a role edit would send the reader to diff `llm_judge.py`
    and find it identical.
    """
    out: list[tuple[str | None, str | None]] = []
    for k in _cache_blob(tool):
        if not isinstance(k, str):
            continue
        if k == prefix:
            out.append((None, None))               # legacy: input-only key
        elif k.startswith(prefix + "|"):
            parts = k[len(prefix) + 1:].split("|")
            out.append((parts[0] or None, parts[1]) if len(parts) == 2
                       else (None, parts[0] or None))
    return out


def _why_rejudged(priors: list[tuple[str | None, str | None]], qid: str, aid: str,
                  base_ref: str, new_ref: str) -> str:
    """Why this exact input, already in the cache, is being re-judged rather than replayed.

    Evaluated in blast-radius order: a changed ASSESSOR re-judges the whole fleet and is
    the reading a spike should be attributed to first; a changed QUESTION re-judges one
    fork. The two shape-widening cases follow, and the final branch is total — a miss the
    other four cannot explain must still say something, not fall through silently.
    """
    q_named = sorted({q for q, _ in priors if q})
    a_named = sorted({a for _, a in priors if a})
    if a_named and aid not in a_named:
        return (f"the assessor changed ({', '.join(a_named)} → {aid}) — the "
                f"cached verdict for {base_ref}→{new_ref} was produced by a "
                f"different judge, so it is being re-judged, not replayed")
    if q_named and qid not in q_named:
        return (f"the judge context changed ({', '.join(q_named)} → {qid}) — this fork's "
                f"`role` is interpolated into the judge's prompt, so the cached verdict "
                f"for {base_ref}→{new_ref} answers a DIFFERENT question; it is being "
                f"re-judged, not replayed")
    if any(q is None and a is not None for q, a in priors):
        return (f"{base_ref}→{new_ref} was cached before the judge context was part of "
                f"the cache identity — re-judging once under context {qid} so the verdict "
                f"records which question it answers")
    if any(a is None for _, a in priors):
        return (f"{base_ref}→{new_ref} was cached before the assessor was part "
                f"of the cache identity — re-judging once under assessor {aid} "
                f"so the verdict is attributable")
    return (f"{base_ref}→{new_ref} has cached verdicts, but none under this exact "
            f"context/assessor pair ({qid}/{aid}) — re-judging, not replaying")


def _cache_put(tool: str, key: str, rep: dict) -> str:
    """Best-effort persist; a cache failure must never break the tick.

    Returns "" when the entry was stored, and the REASON otherwise — a silent no-op here
    would be indistinguishable from a hit, and the whole point of the #12 refusal is that
    it is visible in the report of the run that was refused.
    """
    if not gk_state.may_write(CACHE):
        return gk_state.refusal_reason(CACHE, f"the {tool} assessment cache")
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        p = CACHE / f"{tool}.json"
        try:
            blob = json.loads(p.read_text())
            if not isinstance(blob, dict):
                blob = {}
        except (OSError, json.JSONDecodeError):
            blob = {}
        blob[key] = rep
        p.write_text(json.dumps(blob, ensure_ascii=False))
    except OSError as e:
        return f"cache write failed: {e}"
    return ""


def assess(tool: str) -> dict:
    """Full per-commit assessment for one tool, from its ledger. Never raises.

    IDEMPOTENT over an unchanged input JUDGED BY AN UNCHANGED ASSESSOR: such a range
    replays the STORED verdict instead of re-judging. Without the input half the daily
    cron re-assessed a static range forever — the magic range 8.3.674→8.3.676 was
    assessed 7 days running (2026-07-19..25), spending an LLM call and opening a fresh
    vibe-ic review PR each day, and (because the judgment was sampled) contradicting its
    own earlier verdicts. Re-judging identical input can only add drift, never
    information — but only while the judge is identical too, which is why `assessor_id()`
    is in the key (vibeic/vibeic-eda#4).
    """
    led_p = LEDGER / f"{tool}.json"
    if not led_p.is_file():
        return {"tool": tool, "error": f"no ledger at {led_p}"}
    try:
        led = json.loads(led_p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"tool": tool, "error": f"bad ledger: {e}"}

    if not led.get("integrated"):
        return {"tool": tool, "status": "not_layered", "commits": []}
    # OWNER RULING (2026-07-29): "daily merge all new commits from upstream for
    # forked tools." A fork with no new RELEASE is not a clean fork — it can be
    # hundreds of commits behind a project that does not tag at all. Returning
    # "clean" for those is what kept OpenROAD (772 behind) out of every
    # assessment ever run. `behind_commits` is already in the ledger.
    #
    # …and a release gap that could not be MEASURED is not a gap of zero. When
    # `behind_releases` is null the ledger is saying containment could not be
    # decided for at least one upstream release; `or 0` maps that onto the same
    # value as "checked, nothing there", and this return is the exact place where
    # that would become a published "CLEAN". Unknown falls through to a real
    # assessment instead, and says so in the result.
    rel_unknown = release_gap_unknown(led)
    # `release_gap` is the only reader: `or 0` was still turning a NOT-PROBED null
    # — no pin, or an upstream with no release and no tag — into a measured zero,
    # and this return is where that becomes a published CLEAN.
    rel_gap = release_gap(led)
    if (not rel_unknown
            and rel_gap == 0
            and (led.get("behind_commits") or 0) == 0):
        return {"tool": tool, "status": "clean", "commits": [],
                "base_release": led.get("base_release"), "latest": led.get("upstream_latest_release")}

    upstream = led["upstream"]
    up_branch = led.get("upstream_default_branch") or "master"
    our_ref = led.get("pinned_ref_full")
    base_ref = led.get("base_release") or (led.get("fork_point") or {}).get("sha")
    new_ref = led.get("upstream_latest_release")

    # vibeic-eda#31, second half. A project that ships from rolling master has no
    # NEW RELEASE to frame the range with, so `base_release ... latest_release`
    # collapses onto a single tag and the assessment covers an empty diff — it
    # reports nothing and looks like a clean fork.
    #
    # OpenROAD is the case: `base_release` and `upstream_latest_release` were
    # BOTH `v0.9.0-beta` (2020-07-06) while master moved daily, so the range was
    # `v0.9.0-beta...v0.9.0-beta`. Reading tags fixed which tag is "latest"; it
    # did not give the range two ENDS. OpenSTA is worse — one tag, ever, from
    # 2020 — so no amount of tag-reading produces a range for it at all.
    #
    # When the release range is empty but master HAS moved, assess the commit
    # range instead: our pinned ref to upstream's default branch. That is exactly
    # the set `behind_commits` counted, which is the set a human would be asked
    # about. Cross-fork compare is used deliberately and was verified against the
    # live API — the upstream repo resolves a sha from its own fork network.
    #
    # An UNKNOWN release gap takes the same branch, and for a stronger reason: the
    # commit range `our pin … upstream default branch` is a SUPERSET of whatever
    # those undecided releases contain, so it is the one range that cannot miss
    # them. What it must never do is fall through as if the gap had been measured
    # at zero — that is `or 0` on a null, which is the defect being fixed.
    if ((rel_unknown or not rel_gap)
            and (led.get("behind_commits") or 0) > 0
            and our_ref):
        base_ref, new_ref = our_ref, up_branch

    if not (base_ref and new_ref):
        return {"tool": tool,
                "error": ("release containment undetermined and no commit range to fall "
                          "back on" if rel_unknown else
                          "missing base_release/latest for the commit range")}

    # Already assessed this exact input, UNDER THIS EXACT QUESTION, WITH THIS EXACT
    # ASSESSOR? Replay it — no LLM, no new PR, no drift. A different assessor is a
    # different judge and a different `role` is a different question; either one misses.
    role = led.get("role", "")
    aid = assessor_id()
    qid = judge_context_id(tool, role)
    prefix = _cache_input_prefix(tool, base_ref, new_ref, our_ref)
    ckey = _cache_key(tool, base_ref, new_ref, our_ref, aid, qid)
    why_reassessed = ""
    if os.environ.get("GK_ASSESS_NOCACHE") not in ("1", "true", "yes"):
        hit = _cache_get(tool, ckey)
        if hit is not None:
            return {**hit, "cached": True, "assessor": aid, "judge_context": qid,
                    "replayed_at": _now_iso()}
        # MISS. If we HAVE judged this same range before, say WHICH input to the verdict
        # moved — widening the key re-judges every cached range exactly once, and an
        # unexplained spike in API calls is how a correct invalidation gets
        # mistaken for a bug.
        priors = _prior_keys(tool, prefix)
        if priors:
            why_reassessed = _why_rejudged(priors, qid, aid, base_ref, new_ref)

    got = upstream_commits(upstream, base_ref, new_ref)
    if isinstance(got, list) and got and got[0].get("_err"):
        return {"tool": tool, "error": f"compare failed: {got[0]['_err']}"}
    commits, agg_files = got
    our_files = our_patch_files(upstream, up_branch, our_ref, tool) if our_ref else set()
    # DETERMINISTIC pre-filter, before any LLM spend: drop the commits our shipped
    # ref already carries (by ancestry OR cherry-pick patch-id). Work that is
    # already done is not a human decision, and re-proposing it is how the same
    # range stayed "2 need review" for 7 days while both were in fact ours.
    carried = already_carried(tool, our_ref, commits) if our_ref else set()
    # A commit with a RECORDED gatekeeper decision is settled too — don't re-judge it.
    decided = recorded_decisions(tool)
    todo = [c for c in commits if c["sha"] not in carried and c["sha"] not in decided]
    # `role` — the same local the cache key was derived from. Two readings of the ledger
    # is how the key and the prompt drift apart again (vibeic/vibeic-eda#11).
    cls_map = classify_commits(tool, role, todo)

    # ── PASS 1: the deterministic probes, per commit ──────────────────────────
    # Everything except whether the JUDGEMENT reproduces. Splitting the loop is what
    # keeps the #6 re-sample proportionate: it is spent on the output of this pass —
    # the commits that already clear every other condition, 1 of 105 on the range that
    # motivated the issue — rather than on the range.
    probes: dict = {}
    for c in commits:
        if c["sha"] in carried or c["sha"] in decided:
            continue
        cls = cls_map.get(c["sha"], _not_assessed("this commit was never sent to the judge"))
        # cheap overlap signal from the aggregate diff isn't per-commit; do a per-commit
        # touch check only for adopt-candidates (bugfix + relevant) to bound gh/git cost.
        cand = cls.get("category") == "bugfix" and cls.get("recommend") == "adopt"
        touches = None
        clean = None
        reach = None
        if cand:
            cf = _commit_files(upstream, c["sha_full"])
            # UNKNOWN on EITHER side (our patch files errored → None, or this commit's files
            # errored → None) must read as "assume overlap" so the conflict gate fails safe.
            touches = True if (our_files is None or cf is None) else bool(our_files & cf)
            clean = clean_cherrypick(tool, our_ref, c["sha_full"]) if our_ref else None
            # The doctrine's "confirm it reproduces in OUR version", as a program
            # (vibeic/vibeic-eda#5). Adopt-candidates only, same cost bound as the
            # probes above.
            reach = _reachability(tool, c["sha_full"])
        probes[c["sha"]] = {"cls": cls, "cand": cand, "touches": touches, "clean": clean,
                            "reach": reach,
                            "pre": bool(cand and _passes_static_gate(cls, touches, clean, reach))}

    # ── PASS 2: ONE confirmation round, over the pre-qualified set only ───────
    # vibeic/vibeic-eda#6. A clearly-safe verdict opens a real cherry-pick PR, and the
    # same 105 commits judged three times returned three different `useful` sets — so
    # the auto-adopt tier, and only it, must rest on agreeing independent samples.
    pre = [c for c in commits if probes.get(c["sha"], {}).get("pre")]
    agreements = _confirm_candidates(tool, role, pre, cls_map)

    # ── PASS 3: rows ──────────────────────────────────────────────────────────
    assessed, safe = [], []
    for c in commits:
        if c["sha"] in carried:
            assessed.append({**c, "category": "carried", "summary":
                             "already in our shipped ref (ancestor or cherry-picked)",
                             "relevant": None, "risk": None, "reproduce": "",
                             "recommend": "carried", "touches_our_patches": None,
                             "clean_cherrypick": None, "decision": "carried"})
            continue
        if c["sha"] in decided:
            rec = decided[c["sha"]]
            assessed.append({**c, "category": "decided",
                             "summary": str(rec.get("reason", ""))[:200],
                             "relevant": None, "risk": None, "reproduce": "",
                             "recommend": rec.get("decision", "skip"),
                             "touches_our_patches": None, "clean_cherrypick": None,
                             "decision": f"recorded:{rec.get('decision', 'skip')}"})
            continue
        p = probes[c["sha"]]
        cls, cand = p["cls"], p["cand"]
        touches, clean, reach = p["touches"], p["clean"], p["reach"]
        agr = agreements.get(c["sha"])
        row = {**c, **{k: cls.get(k) for k in
                       ("category", "summary", "relevant", "risk", "reproduce", "recommend")},
               "touches_our_patches": touches, "clean_cherrypick": clean,
               "reachability": reach, "agreement": agr}
        if reach is not None and reach.get("verdict") == "unreachable":
            # DISCLOSE the disagreement, do not resolve it silently. The judge's reason
            # travels into an auto-opened merge PR, so a reason the analysis contradicts
            # must never be printed on its own — the marker leads so the summary column's
            # truncation cannot hide it.
            row["judge_summary"] = row.get("summary") or ""
            row["reachability_conflict"] = True
            row["summary"] = (f"⚠ UNREACHABLE FROM OUR SURFACE — {reach.get('detail', '')} · "
                              f"the judge nonetheless called it relevant: "
                              f"\"{row['judge_summary']}\"")
        elif isinstance(agr, dict) and agr.get("agree") is not True:
            # Same fail-closed shape as the reachability disagreement above: BOTH
            # readings are printed and NEITHER is resolved away. Averaging to a majority
            # and saying nothing is the failure this fixes — the row must state that the
            # judgement did not reproduce, because that fact is what a human is being
            # asked to decide. (A commit reaching here already passed reachability, so
            # the two markers can never contend for the same summary cell.)
            row["judge_summary"] = row.get("summary") or ""
            row["sampling_conflict"] = True
            row["summary"] = (f"⚠ JUDGEMENT DID NOT REPRODUCE — {agr.get('detail', '')} · "
                              f"the first reading's stated reason was: "
                              f"\"{row['judge_summary']}\"")
        if cand and _clearly_safe(cls, touches, clean, reach, agr):
            row["decision"] = "auto-safe"
            safe.append(row["sha"])
        else:
            row["decision"] = "human"
        assessed.append(row)

    rep = {"tool": tool, "status": "assessed", "upstream": upstream,
           "base_release": base_ref, "latest": new_ref,
           # PROVENANCE — who judged this, WHAT THEY WERE ASKED, and when. Carried into
           # the cache so a replayed report can say it was restored, by whom it was
           # decided, and under which question (vibeic/vibeic-eda#11: a verdict reached
           # under a role we have since reworded is not the verdict the report implies).
           "assessor": aid, "judge_context": qid, "judge_role": role,
           "assessed_at": _now_iso(),
           "our_ref": (our_ref or "")[:12],
           "our_patch_files": (len(our_files) if our_files is not None else None),
           "commit_count": len(commits), "aggregate_files": len(agg_files),
           "carried": sorted(carried), "clearly_safe": safe, "commits": assessed}
    # Nothing left to decide: every upstream commit is either already ours or has
    # been triaged. Say so explicitly — a range whose only outstanding item is a
    # deliberate SKIP is DECIDED, not pending, and must not read as open work.
    rep["decided"] = sorted(s for s in decided if any(c["sha"] == s for c in commits))
    # A RECOMMENDATION IS NOT A DECISION. This read
    # `decision == "human" and recommend != "skip"`, on the reasoning in the comment
    # above — that a deliberate SKIP is settled, not open. True, and it describes a
    # different value: a deliberate skip is `decision == "recorded:skip"`, which the
    # `decided` bucket already counts. What the excluded rows carry is the ASSESSOR's
    # suggestion while the decision column still says `human` — nobody has decided
    # them.
    #
    # MEASURED on the 2026-08-04 tick, headline vs its own table:
    #     cocotb      17 claimed   61 rows marked human
    #     open_pdks    9 claimed   15 rows marked human
    #     slang        0 claimed    1 row  marked human
    # 51 commits the assessor itself would not settle, absent from every summary a
    # human reads. slang is the shape at its clearest: its file says nothing needs
    # review, so the tool is skipped, and its one commit is marked human.
    #
    # The exclusion sat INSIDE the single owned derivation, so unifying three
    # disagreeing counts (the incident recorded above `_ROW_PREDICATES`) made all
    # three readers agree on the same understated number. Consistency was reached;
    # correctness was not, and agreement is what stopped anyone looking.
    rep["outstanding"] = [c["sha"] for c in assessed if c["decision"] == "human"]
    # Kept so the documents can still lead with what the assessor would adopt without
    # hiding what it would not — the triage signal the exclusion was reaching for.
    rep["outstanding_rec_adopt"] = [c["sha"] for c in assessed
                                    if c["decision"] == "human"
                                    and c.get("recommend") != "skip"]
    # DISCLOSURE: the commits the judge never reached a conclusion about. Without this the
    # report cannot distinguish "judged, and unremarkable" from "never judged" — which is
    # exactly how a truncated reply published itself as 105 high-risk findings.
    rep["not_assessed"] = [c["sha"] for c in assessed if c.get("category") == NOT_ASSESSED]
    # DISCLOSURE: adopt-candidates the model called relevant that nothing we run reaches.
    # These are NOT clearly-safe — they are human decisions, with both statements on the row.
    rep["unreachable"] = [c["sha"] for c in assessed if c.get("reachability_conflict")]
    # DISCLOSURE: adopt-candidates whose judgement did NOT reproduce across independent
    # samples (vibeic/vibeic-eda#6). Also human decisions, also with every reading printed.
    rep["unconfirmed"] = [c["sha"] for c in assessed if c.get("sampling_conflict")]
    # How many judgements each auto-adopt candidate had to survive — so a reader of an
    # archived report can tell a once-confirmed verdict from a twice-confirmed one
    # without reconstructing the environment it ran under.
    rep["judge_samples"] = len(next((a.get("readings") or [] for a in agreements.values()
                                     if a.get("readings")), [])) or None
    # Only a COMPLETE assessment is cacheable. If ANY commit came back NOT ASSESSED the
    # verdict is provisional — caching it would freeze a transient API outage, or a reply
    # that got cut off at the output cap, into a permanent record that never re-resolves.
    # A confirmation round that did not COMPLETE is provisional for exactly that reason:
    # "one sample never arrived" is an outage, not a finding, and freezing it would make
    # a transient failure permanently demote a commit with no way back. A genuine
    # DISAGREEMENT is a finding and does cache.
    if not any(c.get("_note") for c in cls_map.values()) \
            and all(a.get("complete") for a in agreements.values()):
        # PROVENANCE (vibeic/vibeic-eda#12) travels INSIDE the stored entry, so a later
        # reader of the cache — or of a replayed report, which carries the stored block
        # verbatim — can say which checkout, which commit and which kind of process put
        # it there. Built as a copy: the block is attached to `rep` only once the write
        # actually happened, so a refused run never returns a report claiming to have
        # written what it was refused.
        prov = gk_state.provenance()
        refused = _cache_put(tool, ckey, {**rep, gk_state.PROVENANCE_KEY: prov})
        if refused:
            rep["cache_write_refused"] = refused
        else:
            rep[gk_state.PROVENANCE_KEY] = prov
    if why_reassessed:
        # Deliberately set AFTER _cache_put: this explains THIS tick's miss and must
        # not be replayed later as though the next reader's tick re-judged anything.
        rep["reassessed_because"] = why_reassessed
    return rep


def _reachability(tool: str, sha_full: str) -> dict | None:
    """The deterministic surface check, or None if the module is unavailable.

    None (module missing) is NOT a demotion — it is the same "could not determine"
    the check itself returns, expressed by the absence of a result, and `_clearly_safe`
    treats it as such.
    """
    try:
        import reachability
    except Exception:  # noqa: BLE001 — a check that will not import must not break the tick
        return None
    try:
        return reachability.check(tool, sha_full)
    except Exception:  # noqa: BLE001
        return None


def _commit_files(upstream: str, sha_full: str) -> set[str] | None:
    if not sha_full:
        return None
    d = _gh(f"repos/{upstream}/commits/{sha_full}")
    if d.get("_err"):
        return None
    return {f.get("filename") for f in (d.get("files") or []) if f.get("filename")}


# ── markdown render (for the PR body) ─────────────────────────────────────────
def _probe_cell(val, yes: str, no: str, settled: bool) -> str:
    """Render one of the two PROBE columns (`conflict`, `clean-pick`).

    None is not a measurement. On a settled row (carried / recorded decision) the probe
    does not apply; on any other row it means the probe DID NOT RUN — both probes are
    computed only for adopt-candidates, to bound gh/git cost. Neither case may render as
    a bare dash, which reads as "checked, nothing to report". `clean-pick` used to print
    `—` for None while `conflict` printed `?` for the same None, so the table disagreed
    with itself about what an unknown looks like.
    """
    if val is True:
        return yes
    if val is False:
        return no
    return "n/a" if settled else "not-probed"


def _reach_cell(reach: dict | None, settled: bool) -> str:
    """Render the `reach` column. Like the two probes, it runs only for adopt-candidates,
    so absence means DID NOT RUN — never "checked, reachable"."""
    if not isinstance(reach, dict):
        return "n/a" if settled else "not-probed"
    v = reach.get("verdict")
    return {"reachable": "✓ ours", "unreachable": "⚠ NOT ours",
            "unknown": "undetermined"}.get(v, "not-probed")


def _agree_cell(agr: dict | None, settled: bool) -> str:
    """Render the `agree` column — did independent judgements of this commit match?

    Like the other probes it runs ONLY for commits that already cleared every other
    clearly-safe condition, so absence means DID NOT RUN. It must never render as
    "confirmed": an unconfirmed verdict is precisely what this column exists to expose.
    """
    if not isinstance(agr, dict):
        return "n/a" if settled else "not-probed"
    n = len(agr.get("readings") or [])
    if agr.get("agree") is True:
        return f"✓ {n}/{n}"
    if not agr.get("complete"):
        return "⚠ incomplete"
    return "⚠ DIVERGED"


# ── ONE derivation of the headline counts (vibeic/vibeic-eda#7) ──────────────
# Every document a tick publishes states the same four numbers, and each site used to
# derive them for itself:
#
#   assess_release.render_md   len(outstanding), else commit_count - safe - carried - decided
#   gatekeeper.py:231-238      len(outstanding), else commit_count - safe
#   pr_notify.open_assessment_pr                  commit_count - safe   (UNCONDITIONALLY —
#                                                 it never read `outstanding` at all)
#
# Three readers of one report, three answers. On the 2026-07-28 magic assessment the
# tick published "2 need human review" in the assessment table, "2" in the daily report
# and "107 need review" in the vibe-ic PR body it opened the same minute — and on a
# cached report predating the `outstanding` field, 104 / 107 / 107. A count nobody owns
# is a count that drifts, so the derivation lives here once and the documents render it.
HEADLINE = ("clearly_safe", "carried", "decided", "outstanding")

# How to recount a category from the per-commit rows when the summary list is missing.
# The rows are the SAME structured record the lists were built from, so this is a second
# reading of the classification, not an estimate — which is why it sits ahead of any
# arithmetic in `summary_counts`.
_ROW_PREDICATES = {
    "clearly_safe": lambda c: c.get("decision") == "auto-safe",
    "carried": lambda c: c.get("decision") == "carried",
    "decided": lambda c: str(c.get("decision") or "").startswith("recorded:"),
    # `decision == "human"` and nothing else: the column means "a human must decide",
    # so every row carrying it is outstanding. Filtering by the assessor's `recommend`
    # here confused a suggestion with a decision — see the note at the `outstanding`
    # list. A settled skip is `recorded:skip`, which `decided` already claims.
    "outstanding": lambda c: c.get("decision") == "human",
    "outstanding_rec_adopt": lambda c: (c.get("decision") == "human"
                                        and c.get("recommend") != "skip"),
    "not_assessed": lambda c: c.get("category") == NOT_ASSESSED,
    "unreachable": lambda c: bool(c.get("reachability_conflict")),
    "unconfirmed": lambda c: bool(c.get("sampling_conflict")),
}


def summary_counts(rep: dict) -> dict:
    """The headline counts of one assessment — the single source every document renders.

    Reads the STRUCTURED value wherever one exists, in descending order of directness:

      1. the summary list itself (`rep["outstanding"]` &c) — exact;
      2. failing that, a recount over `rep["commits"]` by the `decision`/`recommend`
         columns — also exact, and available on every report ever cached, because the
         rows are what the lists were derived from in the first place;
      3. only when BOTH are absent, arithmetic — and then `derived` is set so the
         documents can say the number was inferred rather than measured.

    Step 2 is the repair. The old fallbacks were reached whenever a summary list was
    missing, and they guessed with subtraction while the exact answer was sitting in the
    rows one key away. Arithmetic-shaped counts are now reachable only for a report that
    carries no rows at all.

    The fail-safe DIRECTION of the old fallback is kept, and is why the arithmetic is
    what it is: an unknown `clearly_safe` counts as 0 (nothing may be claimed safe when
    we cannot tell), an unknown `carried`/`decided` counts as 0 (nothing may be claimed
    already-resolved), and `outstanding` is whatever those leave over — so uncertainty
    always reads as "needs review", never as "nothing to do".
    """
    rows = rep.get("commits")
    rows = rows if isinstance(rows, list) else []
    cc = rep.get("commit_count")
    cc = cc if isinstance(cc, int) else len(rows)
    out: dict = {"commits": cc}
    derived = []
    for name, pred in _ROW_PREDICATES.items():
        v = rep.get(name)
        if isinstance(v, list):                       # 1. the summary list
            out[name] = len(v)
        elif rows:                                    # 2. recount from the rows
            out[name] = sum(1 for c in rows if isinstance(c, dict) and pred(c))
        else:                                         # 3. unknown → fail-safe
            out[name] = None
            derived.append(name)
    for name in ("clearly_safe", "carried", "decided", "not_assessed",
                 "unreachable", "unconfirmed"):
        if out[name] is None:
            out[name] = 0
    if out["outstanding"] is None:
        out["outstanding"] = max(cc - out["clearly_safe"] - out["carried"] - out["decided"], 0)
    out["derived"] = sorted(derived)
    return out


# Each document phrases the counts for its own reader; the numbers are parsed back out
# of the RENDERED TEXT so the check tests what was published, not the ints that were
# meant to be. A formatter that drops a field, or a caller that pairs a rendered table
# with a different report, is exactly what this catches.
_SUMMARY_SENTENCE = re.compile(
    r"(?P<clearly_safe>\d+) clearly-safe,\s*(?P<carried>\d+) already carried,\s*"
    r"(?P<decided>\d+) previously decided,\s*(?P<outstanding>\d+) need human review")
_HEADLINE_RE = {
    "assessment": re.compile(
        r"Already carried:\s*(?P<carried>\d+)\*?\*?.*?"
        r"decided \(recorded\):\s*(?P<decided>\d+)\*?\*?.*?"
        r"clearly-safe to auto-adopt:\s*(?P<clearly_safe>\d+)\*?\*?.*?"
        r"needs human decision:\s*(?P<outstanding>\d+)", re.S),
    # The daily report and the PR body state it in one sentence, deliberately the SAME
    # sentence: the PR body used to say "N need review" while the report said "N need
    # human review", so a reader comparing them had to decide whether two different
    # phrasings were two different questions before noticing they were two different
    # answers. One wording, one pattern.
    "report": _SUMMARY_SENTENCE,
    "pr": _SUMMARY_SENTENCE,
}
# The daily report's "nothing left to do" phrasing states no clearly-safe/outstanding
# number because both are zero; parse it as the zeros it asserts rather than as "no
# counts present", or the one shape that claims a range is SETTLED goes unchecked.
_RESOLVED_RE = re.compile(
    r"(?P<carried>\d+) already carried,\s*(?P<decided>\d+) previously decided"
    r"\s*—\s*nothing outstanding")


def parse_headline(kind: str, text: str) -> dict | None:
    """Recover the four headline counts from a rendered document, `kind` selecting the
    phrasing. None means THIS PARSER FOUND NONE — which is two different facts.

    A document may carry no counts because it has none to carry (an error note, a
    clean/not-layered stub), or because it states them in a shape no longer in
    `_HEADLINE_RE`. Nothing in the returned value tells the two apart, so no caller may
    read None as "states no counts" — see `states_counts`, which answers that from the
    REPORT instead (vibeic/vibeic-eda#9).
    """
    text = text or ""
    m = _HEADLINE_RE[kind].search(text)
    if m:
        return {k: int(v) for k, v in m.groupdict().items()}
    if kind in ("report", "pr"):
        m = _RESOLVED_RE.search(text)
        if m:
            return {"clearly_safe": 0, "outstanding": 0,
                    "carried": int(m.group("carried")), "decided": int(m.group("decided"))}
    return None


# The provenance banner `_provenance_lines` writes, read back. Both phrasings ("Computed
# on <t> by assessor `<id>`" and the REPLAYED banner's "COMPUTED on <t> by assessor
# `<id>`") name the tick that DECIDED the verdict, which is the identity that has to
# match — a replayed report and the report it replays are the same judgement.
_PROVENANCE_RE = re.compile(
    r"COMPUTED\s+on\s+(?P<assessed_at>\S+?)\s+by\s+assessor\s+`(?P<assessor>[^`]+)`",
    re.I)


def parse_provenance(md: str) -> dict:
    """{assessor, assessed_at} recovered from a rendered assessment, {} if it says
    neither (a report predating provenance pinning)."""
    m = _PROVENANCE_RE.search(md or "")
    if not m:
        return {}
    got = m.groupdict()
    if got.get("assessor", "").startswith("unknown"):
        got.pop("assessor")
    return {k: v for k, v in got.items() if v}


def states_counts(rep: dict) -> bool:
    """Does this report HAVE headline counts for its documents to state?

    The skip in `cross_check` has to be decided here — from the STRUCTURE of the report —
    rather than from whether a regex matched the text (vibeic/vibeic-eda#9). Two shapes
    legitimately render a stub with no numbers in it, and both are knowable without
    reading a character of the render: an entry that ERRORED (`render_md` emits
    "assessment error — …") and a fork that is CLEAN or NOT LAYERED ("nothing to
    assess"). Everything else is an assessed range, and every document it produces states
    four numbers — so a document of one that does not parse is a defect, not a stub.
    """
    return not rep.get("error") and rep.get("status") in (None, "assessed")


def cross_check(rep: dict, documents: dict[str, str]) -> list[str]:
    """Do the documents this tick is about to publish agree with each other?

    `documents` maps a phrasing kind (`assessment` / `report` / `pr`) to the RENDERED
    text. Every one is parsed back and compared with `summary_counts(rep)`. Returns []
    on agreement, else one line per disagreeing field — the caller's job is to refuse to
    publish, not to reconcile. A tick that emits a report and an assessment whose counts
    contradict each other has already lost the property that makes either one worth
    reading, and picking a winner in code would hide which reader was wrong.

    A report that states no counts at all — an assessment error, a clean/not-layered fork
    — is skipped: there is nothing to disagree with. That skip is `states_counts(rep)`,
    taken BEFORE anything is parsed, and it used to be taken after: an unparseable
    document was read as one that stated nothing (vibeic/vibeic-eda#9). Since the four
    numbers reach the reader through three separate renders (`render_md`,
    `gatekeeper.assessment_entry`, `pr_notify.tally_line`) and only `_HEADLINE_RE`'s
    current wording connects them to this check, that made rewording a headline a silent
    disarm — the guard skipped the one document whose phrasing had drifted, which is
    exactly the document nothing else was checking. Measured on the line that shipped in
    vibeic/vibe-ic#508 ("… — 0 clearly-safe, 108 need review", against an assessment
    saying 1 and 2): `parse_headline` returned None and `cross_check` returned []. For an
    assessed report, UNREADABLE IS A FAILURE.
    """
    if not states_counts(rep):
        return []
    want = summary_counts(rep)
    bad = []
    for kind, text in sorted(documents.items()):
        got = parse_headline(kind, text)
        if got is None:
            bad.append(
                f"{rep.get('tool', '?')}: the {kind} document states no counts this "
                f"program can read back, but the assessment it renders states "
                + ", ".join(f"{f}={want[f]}" for f in HEADLINE) +
                f" — the render and parse_headline('{kind}') have drifted apart, so "
                f"this document would be published unchecked")
            continue
        for field in HEADLINE:
            if got[field] != want[field]:
                bad.append(f"{rep.get('tool', '?')}: the {kind} document says "
                           f"{field}={got[field]}, the assessment it renders says "
                           f"{want[field]}")
    return bad


def _provenance_lines(rep: dict) -> list[str]:
    """Was this judgement COMPUTED on this tick, or RESTORED from an earlier one?

    vibeic/vibeic-eda#4: a replayed verdict used to be byte-indistinguishable from a
    freshly computed one — in the report, in the PR body and in the `[assess]` log
    line. Only the tick's stdout said "replayed from cache", and nobody reads a cron
    log a week later while reading `2026-07-28-magic.md`. The report itself has to
    carry it, together with the identity of the judge that decided it.
    """
    aid = rep.get("assessor")
    when = rep.get("assessed_at")
    # The QUESTION the verdict answers (vibeic/vibeic-eda#11). Rendered only when the
    # report carries it: an archived report that predates the field must not be made to
    # look as though it recorded one.
    qid = rep.get("judge_context")
    # The role is CONFIGURATION prose rendered into a markdown blockquote: a newline would
    # end the quote mid-sentence and a backtick would open a code span that swallows the
    # rest of the banner. Flattened and bounded here rather than trusted, the same way
    # every judge-supplied `summary` is.
    role = " ".join(str(rep.get("judge_role") or "").replace("`", "'").split())[:200]
    asked = (f" asked as `{qid}`" + (f" (role: {role})" if role else "")) if qid else ""
    if rep.get("cached"):
        return [f"> **⟲ REPLAYED FROM CACHE — no classifier ran for this report.** The "
                f"judgement below was COMPUTED "
                f"{('on ' + when) if when else 'on an unrecorded earlier tick'} by assessor "
                f"`{aid or 'unknown — this report predates assessor pinning'}`"
                f"{asked} and RESTORED "
                f"{('on ' + rep['replayed_at']) if rep.get('replayed_at') else 'on this tick'}, "
                f"because the upstream range, our carried-patch ref, the question this fork's "
                f"`role` puts to the judge AND the assessor are all unchanged. Nothing in this "
                f"table was re-judged today.", ""]
    n = rep.get("judge_samples")
    how = (f" Each auto-adopt candidate had to survive {n} independent judgements."
           if n else "")
    under = asked or " under this fork's recorded role"
    return [f"> Computed {('on ' + when) if when else 'on this tick'} by assessor "
            f"`{aid or 'unrecorded'}` — a content hash of the judge module, its system prompt, "
            f"the model id, the chunk size and the sample count —{under}. Change any of them, "
            f"or the `role` that question is built from, and this range is re-judged rather "
            f"than replayed.{how}", ""]


def _derived_lines(counts: dict) -> list[str]:
    """A count that had to be INFERRED must say so where it is read.

    `summary_counts` reaches arithmetic only for a report carrying neither the summary
    list nor the rows — nothing this codebase writes today, but archived reports are
    read years later and a number that was computed by subtraction is a different claim
    from one that was counted. Saying which is which is the whole reason the fail-safe
    direction is safe to keep: an inferred count that over-states the work is honest
    only while the reader knows it was inferred.
    """
    if not counts.get("derived"):
        return []
    return [f"> **⚠ INFERRED, not counted: {', '.join(counts['derived'])}.** This report "
            "carries neither the summary list nor the per-commit rows those categories are "
            "counted from, so the numbers above are arithmetic over the categories that ARE "
            "present. They err toward 'needs review'.", ""]


def render_md(rep: dict) -> str:
    tool = rep.get("tool", "?")
    if rep.get("error"):
        return f"### {tool}: assessment error — {rep['error']}\n"
    if rep.get("status") in ("clean", "not_layered"):
        return f"### {tool}: {rep['status']} — nothing to assess.\n"
    n = summary_counts(rep)
    n_carried, n_decided = n["carried"], n["decided"]
    n_safe, n_open = n["clearly_safe"], n["outstanding"]
    n_open_adopt = min(n.get("outstanding_rec_adopt") or 0, n_open)
    L = [f"## {tool} — selective-merge assessment",
         f"Range **{rep['base_release']} → {rep['latest']}** · {n['commits']} upstream "
         f"commit(s) · our branch carries patches over "
         f"{rep['our_patch_files'] if rep.get('our_patch_files') is not None else '?'} file(s).",
         f"**Already carried: {n_carried}** · **decided (recorded): {n_decided}** · "
         f"**clearly-safe to auto-adopt: {n_safe}** · **needs human decision: {n_open}**"
         + (f" ({n_open_adopt} the assessor would adopt · {n_open - n_open_adopt} it "
            f"would skip — its recommendation, not a decision)" if n_open else ""), ""]
    L += _provenance_lines(rep)
    L += _derived_lines(n)
    # An assessment that did not complete must SAY SO, above the table, before a reader
    # starts triaging cells that no classifier ever filled in.
    n_na = n["not_assessed"]
    if n_na:
        L += [f"> **⚠ THE JUDGE DID NOT COMPLETE — {n_na} of {n['commits']} commit(s) were "
              "NOT ASSESSED.** Their `cat` / `risk` / `rel` cells read `not-assessed`: no "
              "classifier reached a conclusion about them. That is an ABSENCE OF ANALYSIS, not "
              "a finding — do not read those rows as triage. `conflict` and `clean-pick` are "
              "`not-probed` on the same rows for a second reason: both probes run only for "
              "adopt-candidates, so for an unassessed commit the our-patch-overlap and "
              "cherry-pick analyses DID NOT RUN either. The per-commit reason is in the "
              "summary column.", ""]
    if (n_carried or n_decided) and not n_open and not n_safe and not n_na:
        L += ["> **This range is DECIDED — no action required.** Every upstream commit is "
              "either already in our shipped ref (as an ancestor or a cherry-pick) or "
              "carries a recorded skip decision in `DECISIONS.json`. `behind_releases` "
              "compares RELEASE TAGS, but selective-merge adopts COMMITS, so the tag stays "
              "behind by design after a selective adoption — being 'behind' a tag is not "
              "the same as owing work.", ""]
    # The doctrine's confirmation step, when it CONTRADICTS the model (vibeic/vibeic-eda#5).
    n_unreach = n["unreachable"]
    if n_unreach:  # DISCLOSED above the table, like the not-assessed banner
        L += [f"> **⚠ MODEL / SURFACE DISAGREEMENT on {n_unreach} commit(s).** The judge called "
              "them relevant; the deterministic reachability check found the symbols they "
              "change are reachable only from commands our emitters never issue. Both "
              "statements are on the row and NEITHER has been resolved away. Such a commit is "
              "NOT clearly-safe and is not auto-proposed — it is a human decision. The verdict "
              "may still be right (a NULL guard in a tool we run headless is harmless); what is "
              "wrong is the EVIDENCE, and the evidence is what travels into a merge PR.", ""]
    # The judgement did not reproduce (vibeic/vibeic-eda#6) — same disclosure shape.
    n_unconf = n["unconfirmed"]
    if n_unconf:
        L += [f"> **⚠ JUDGEMENT DID NOT REPRODUCE on {n_unconf} commit(s).** Each had already "
              "cleared every other auto-adopt condition, so it was re-judged by independent "
              "samples of the same commit text — and they did not return the same verdict (or "
              "one of them never arrived). EVERY reading is on the row; none has been averaged "
              "into a majority. Such a commit is NOT clearly-safe and is not auto-proposed. "
              "Measured 2026-07-28: one 105-commit range judged three times at temperature=0 "
              "returned three different useful sets, all divergence on borderline commits — a "
              "verdict only one sample supports is a coin toss, and this tier opens a real "
              "cherry-pick PR.", ""]
    L += [
         "| sha | cat | risk | rel | conflict | clean-pick | reach | agree | rec | decision | "
         "summary |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in rep["commits"]:
        # A row is SETTLED (carried / recorded decision) or NOT ASSESSED or judged. Only
        # the last kind has measurements, and the other two must say which they are —
        # a bare "?" cannot tell a reader "does not apply" from "we never looked".
        settled = c.get("category") in ("carried", "decided")
        blank = "n/a" if settled else (NOT_ASSESSED if c.get("category") == NOT_ASSESSED else "?")
        L.append("| `{sha}` | {category} | {risk} | {rel} | {conf} | {clean} | {reach} | "
                 "{agree} | {recommend} | **{decision}** | {summary} |".format(
                     sha=c["sha"], category=c.get("category") or "?",
                     risk=c.get("risk") or blank,
                     rel={True: "yes", False: "no"}.get(c.get("relevant"), blank),
                     conf=_probe_cell(c.get("touches_our_patches"), "⚠", "—", settled),
                     clean=_probe_cell(c.get("clean_cherrypick"), "✓", "✗", settled),
                     reach=_reach_cell(c.get("reachability"), settled),
                     agree=_agree_cell(c.get("agreement"), settled),
                     recommend=c.get("recommend") or "?", decision=c.get("decision"),
                     # 110, not 80: the not-assessed reason is the whole point of that row,
                     # and at 80 it was cut mid-word ("...stop_reason=max_token"). A
                     # model/surface disagreement gets more still — it has to carry BOTH
                     # statements, and at 110 it was cut before the half that contradicts
                     # ("…reachable only from `" — the reader never sees WHICH command,
                     # nor that we do not issue it). A sampling disagreement is the same
                     # kind of row for the same reason: it carries EVERY reading, and a
                     # disclosure truncated before the second reading discloses nothing.
                     summary=(c.get("summary") or c.get("title") or "")[
                         :600 if (c.get("reachability_conflict") or c.get("sampling_conflict"))
                         else 110].replace("|", "\\|")))
    repro = [c for c in rep["commits"] if c.get("reproduce")]
    if repro:
        L += ["", "### Reproduce-before-adopt (bugfixes)"]
        for c in repro:
            L.append(f"- `{c['sha']}` {c.get('summary') or c['title']} — **reproduce:** {c['reproduce']}")
    L += ["", "> Column notes: `conflict` (does it touch a file our carried patches touch), "
          "`clean-pick` (does it cherry-pick cleanly onto our branch) and `reach` (can any command "
          "our emitters issue reach the symbols it changes) are computed ONLY for adopt-candidates, "
          "to bound gh/git cost. `agree` is narrower still — it runs only for commits that already "
          "cleared EVERY other auto-adopt condition, which is why re-judging costs a couple of "
          "extra requests rather than a multiple of the range. `not-probed` means that analysis "
          "did not run — it is never evidence of no conflict, and on `agree` it is never evidence "
          "the verdict reproduced. `n/a` means the row is already settled (carried, or a recorded "
          "decision). `reach` = `undetermined` means the check could not decide — which is NOT "
          "'unreachable', and leaves the model's verdict standing; `agree` has no such state, "
          "because an unconfirmed verdict is exactly the thing that must not auto-adopt.",
          "", "> Doctrine: understand every commit, confirm each bugfix reproduces in OUR version, "
          "adopt selectively. The `reach` column is that confirmation step as a PROGRAM (no model "
          "involved): it reads the symbols the patch changes, walks callers up to the tool's own "
          "command registry, and compares the result against the commands our emitters actually "
          "issue. The clearly-safe subset (self-contained low-risk bugfix, relevant, no overlap "
          "with our patches, clean cherry-pick, not contradicted by the reachability check, and "
          "whose judgement REPRODUCED across independent samples) may be auto-adopted once "
          "enabled; everything else is a human decision."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rep = assess(args[0]) if args else {"error": "usage: assess_release.py <tool> [--json]"}
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(render_md(rep))
