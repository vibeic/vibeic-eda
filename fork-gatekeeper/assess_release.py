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
patches, cherry-picks clean) is flagged and drives what prepare_merge_pr proposes.
Everything else is a human decision. Doctrine: understand + verify + adopt selectively;
never grab-and-paste.

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

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
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
def _clearly_safe(cls: dict, touches_our_files: bool, clean_pick: bool | None) -> bool:
    """The narrow gate for auto-adopt: an unambiguous, self-contained, relevant, low-risk
    bugfix that does NOT overlap our patches and cherry-picks cleanly. Anything less → human."""
    return (cls.get("category") == "bugfix"
            and cls.get("risk") == "low"
            and cls.get("relevant") is True
            and cls.get("recommend") == "adopt"
            and not touches_our_files
            and clean_pick is True)


CACHE = STATE / "assessment-cache"


def _cache_key(tool: str, base_ref: str, new_ref: str, our_ref: str | None) -> str:
    """Identity of an assessment INPUT: the tool, the upstream range, and the ref our
    carried patches sit on. Same key ⇒ nothing we assess over has changed."""
    return f"{tool}|{base_ref}|{new_ref}|{(our_ref or '')[:12]}"


def _cache_get(tool: str, key: str) -> dict | None:
    try:
        blob = json.loads((CACHE / f"{tool}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rep = blob.get(key)
    return rep if isinstance(rep, dict) else None


def _cache_put(tool: str, key: str, rep: dict) -> None:
    """Best-effort persist; a cache failure must never break the tick."""
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
    except OSError:
        pass


def assess(tool: str) -> dict:
    """Full per-commit assessment for one tool, from its ledger. Never raises.

    IDEMPOTENT over an unchanged input: an upstream range we have already assessed,
    on the same carried-patch ref, replays the STORED verdict instead of re-judging.
    Without this the daily cron re-assessed a static range forever — the magic range
    8.3.674→8.3.676 was assessed 7 days running (2026-07-19..25), spending an LLM call
    and opening a fresh vibe-ic review PR each day, and (because the judgment was
    sampled) contradicting its own earlier verdicts. Re-judging identical input can
    only add drift, never information.
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
    if (led.get("behind_releases") or 0) == 0:
        return {"tool": tool, "status": "clean", "commits": [],
                "base_release": led.get("base_release"), "latest": led.get("upstream_latest_release")}

    upstream = led["upstream"]
    up_branch = led.get("upstream_default_branch") or "master"
    our_ref = led.get("pinned_ref_full")
    base_ref = led.get("base_release") or (led.get("fork_point") or {}).get("sha")
    new_ref = led.get("upstream_latest_release")
    if not (base_ref and new_ref):
        return {"tool": tool, "error": "missing base_release/latest for the commit range"}

    # Already assessed this exact input? Replay it — no LLM, no new PR, no drift.
    ckey = _cache_key(tool, base_ref, new_ref, our_ref)
    if os.environ.get("GK_ASSESS_NOCACHE") not in ("1", "true", "yes"):
        hit = _cache_get(tool, ckey)
        if hit is not None:
            return {**hit, "cached": True}

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
    cls_map = classify_commits(tool, led.get("role", ""), todo)

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
        cls = cls_map.get(c["sha"], _not_assessed("this commit was never sent to the judge"))
        # cheap overlap signal from the aggregate diff isn't per-commit; do a per-commit
        # touch check only for adopt-candidates (bugfix + relevant) to bound gh/git cost.
        cand = cls.get("category") == "bugfix" and cls.get("recommend") == "adopt"
        touches = None
        clean = None
        if cand:
            cf = _commit_files(upstream, c["sha_full"])
            # UNKNOWN on EITHER side (our patch files errored → None, or this commit's files
            # errored → None) must read as "assume overlap" so the conflict gate fails safe.
            touches = True if (our_files is None or cf is None) else bool(our_files & cf)
            clean = clean_cherrypick(tool, our_ref, c["sha_full"]) if our_ref else None
        row = {**c, **{k: cls.get(k) for k in
                       ("category", "summary", "relevant", "risk", "reproduce", "recommend")},
               "touches_our_patches": touches, "clean_cherrypick": clean}
        if cand and _clearly_safe(cls, touches, clean):
            row["decision"] = "auto-safe"
            safe.append(row["sha"])
        else:
            row["decision"] = "human"
        assessed.append(row)

    rep = {"tool": tool, "status": "assessed", "upstream": upstream,
           "base_release": base_ref, "latest": new_ref,
           "our_ref": (our_ref or "")[:12],
           "our_patch_files": (len(our_files) if our_files is not None else None),
           "commit_count": len(commits), "aggregate_files": len(agg_files),
           "carried": sorted(carried), "clearly_safe": safe, "commits": assessed}
    # Nothing left to decide: every upstream commit is either already ours or has
    # been triaged. Say so explicitly — a range whose only outstanding item is a
    # deliberate SKIP is DECIDED, not pending, and must not read as open work.
    rep["decided"] = sorted(s for s in decided if any(c["sha"] == s for c in commits))
    rep["outstanding"] = [c["sha"] for c in assessed
                          if c["decision"] == "human" and c.get("recommend") != "skip"]
    # DISCLOSURE: the commits the judge never reached a conclusion about. Without this the
    # report cannot distinguish "judged, and unremarkable" from "never judged" — which is
    # exactly how a truncated reply published itself as 105 high-risk findings.
    rep["not_assessed"] = [c["sha"] for c in assessed if c.get("category") == NOT_ASSESSED]
    # Only a COMPLETE assessment is cacheable. If ANY commit came back NOT ASSESSED the
    # verdict is provisional — caching it would freeze a transient API outage, or a reply
    # that got cut off at the output cap, into a permanent record that never re-resolves.
    if not any(c.get("_note") for c in cls_map.values()):
        _cache_put(tool, ckey, rep)
    return rep


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


def render_md(rep: dict) -> str:
    tool = rep.get("tool", "?")
    if rep.get("error"):
        return f"### {tool}: assessment error — {rep['error']}\n"
    if rep.get("status") in ("clean", "not_layered"):
        return f"### {tool}: {rep['status']} — nothing to assess.\n"
    n_carried = len(rep.get("carried") or [])
    n_decided = len(rep.get("decided") or [])
    n_safe = len(rep.get("clearly_safe") or [])
    n_open = len(rep.get("outstanding", [])) if "outstanding" in rep else \
        rep["commit_count"] - n_safe - n_carried - n_decided
    L = [f"## {tool} — selective-merge assessment",
         f"Range **{rep['base_release']} → {rep['latest']}** · {rep['commit_count']} upstream "
         f"commit(s) · our branch carries patches over "
         f"{rep['our_patch_files'] if rep.get('our_patch_files') is not None else '?'} file(s).",
         f"**Already carried: {n_carried}** · **decided (recorded): {n_decided}** · "
         f"**clearly-safe to auto-adopt: {n_safe}** · **needs human decision: {n_open}**", ""]
    # An assessment that did not complete must SAY SO, above the table, before a reader
    # starts triaging cells that no classifier ever filled in.
    n_na = len(rep["not_assessed"]) if "not_assessed" in rep else \
        len([c for c in rep.get("commits") or [] if c.get("category") == NOT_ASSESSED])
    if n_na:
        L += [f"> **⚠ THE JUDGE DID NOT COMPLETE — {n_na} of {rep['commit_count']} commit(s) were "
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
    L += [
         "| sha | cat | risk | rel | conflict | clean-pick | rec | decision | summary |",
         "|---|---|---|---|---|---|---|---|---|"]
    for c in rep["commits"]:
        # A row is SETTLED (carried / recorded decision) or NOT ASSESSED or judged. Only
        # the last kind has measurements, and the other two must say which they are —
        # a bare "?" cannot tell a reader "does not apply" from "we never looked".
        settled = c.get("category") in ("carried", "decided")
        blank = "n/a" if settled else (NOT_ASSESSED if c.get("category") == NOT_ASSESSED else "?")
        L.append("| `{sha}` | {category} | {risk} | {rel} | {conf} | {clean} | {recommend} | "
                 "**{decision}** | {summary} |".format(
                     sha=c["sha"], category=c.get("category") or "?",
                     risk=c.get("risk") or blank,
                     rel={True: "yes", False: "no"}.get(c.get("relevant"), blank),
                     conf=_probe_cell(c.get("touches_our_patches"), "⚠", "—", settled),
                     clean=_probe_cell(c.get("clean_cherrypick"), "✓", "✗", settled),
                     recommend=c.get("recommend") or "?", decision=c.get("decision"),
                     # 110, not 80: the not-assessed reason is the whole point of that row,
                     # and at 80 it was cut mid-word ("...stop_reason=max_token").
                     summary=(c.get("summary") or c.get("title") or "")[:110].replace("|", "\\|")))
    repro = [c for c in rep["commits"] if c.get("reproduce")]
    if repro:
        L += ["", "### Reproduce-before-adopt (bugfixes)"]
        for c in repro:
            L.append(f"- `{c['sha']}` {c.get('summary') or c['title']} — **reproduce:** {c['reproduce']}")
    L += ["", "> Column notes: `conflict` (does it touch a file our carried patches touch) and "
          "`clean-pick` (does it cherry-pick cleanly onto our branch) are computed ONLY for "
          "adopt-candidates, to bound gh/git cost. `not-probed` means that analysis did not run — "
          "it is never evidence of no conflict. `n/a` means the row is already settled (carried, "
          "or a recorded decision).",
          "", "> Doctrine: understand every commit, confirm each bugfix reproduces in OUR version, "
          "adopt selectively. The clearly-safe subset (self-contained low-risk bugfix, relevant, no "
          "overlap with our patches, clean cherry-pick) may be auto-adopted once enabled; everything "
          "else is a human decision."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rep = assess(args[0]) if args else {"error": "usage: assess_release.py <tool> [--json]"}
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(render_md(rep))
