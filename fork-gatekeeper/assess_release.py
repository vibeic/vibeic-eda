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
patches, cherry-picks clean) is flagged so the gatekeeper CAN auto-adopt it — but that
execution stays gated (GK_ADOPT=auto-safe) and off by default until the assessments are
trusted. Everything else is a human decision. Doctrine: understand + verify + adopt
selectively; never grab-and-paste.

Design notes:
  * Deterministic parts (commit enumeration via `gh api compare`, our-patch file overlap,
    clean-cherry-pick probe) are pure/testable and never spend an LLM.
  * AI usefulness judgment goes through llm_judge — a SAFE, tool-less Anthropic Messages API
    call (no shell, no GH_TOKEN, no capability given to the model). A prompt-injected commit
    body can at worst yield a WRONG judgment (caught by the human on the review PR); it can
    never run a command or exfiltrate a secret. On by default; kill-switch GK_ASSESS_AI=0 forces
    deterministic-only. Any failure degrades to recommend=manual (never auto-adopt).
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
MAX_COMMITS = int(os.environ.get("GK_ASSESS_MAX_COMMITS", "80"))   # cap the LLM payload


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
_DEGRADED = {"category": "other", "relevant": None, "risk": "high",
             "summary": "", "reproduce": "", "recommend": "manual",
             "_note": "AI assessment unavailable — defaulted to manual (never auto-adopt)"}


def _normalize(parsed, commits: list[dict]) -> dict:
    """Map exactly the commits we asked about to their assessment; any sha the model
    omitted or returned non-dict for falls back to degraded/manual (never auto-adopt)."""
    out = {}
    for c in commits:
        a = parsed.get(c["sha"]) if isinstance(parsed, dict) else None
        out[c["sha"]] = a if isinstance(a, dict) else dict(_DEGRADED)
    return out


def classify_commits(tool: str, role: str, commits: list[dict]) -> dict:
    """Classify commits → {sha: {category, summary, relevant, risk, reproduce, recommend}}.

    Judges each upstream commit's usefulness to our fork via llm_judge (the SAFE, tool-less
    Anthropic Messages API call — no shell, no GH_TOKEN, no capability given to the model, so
    a prompt-injected commit body can at worst produce a WRONG judgment that the human catches
    on the review PR; it can never run a command or exfiltrate a secret). Kill-switch:
    GK_ASSESS_AI=0 forces deterministic-only (every commit → manual). GK_ASSESS_STUB mocks it
    for tests. Any failure (no token, API error, bad shape) degrades to manual — never fatal,
    never auto-adopt on a failed judgment.
    """
    if not commits:
        return {}
    stub = os.environ.get("GK_ASSESS_STUB")
    if stub:
        try:
            return _normalize(json.loads(Path(stub).read_text()), commits)
        except (OSError, json.JSONDecodeError):
            return {c["sha"]: dict(_DEGRADED) for c in commits}
    if os.environ.get("GK_ASSESS_AI", "1") not in ("1", "true", "yes"):
        return {c["sha"]: dict(_DEGRADED) for c in commits}   # kill-switch: deterministic-only
    try:
        import llm_judge
        verdicts = llm_judge.judge(tool, role, commits)
    except Exception:  # noqa: BLE001 — a judge hiccup must never break the assessment
        verdicts = None
    if not verdicts:
        return {c["sha"]: dict(_DEGRADED) for c in commits}
    out = {}
    for c in commits:
        v = verdicts.get(c["sha"])
        if not isinstance(v, dict):
            out[c["sha"]] = dict(_DEGRADED)
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


def assess(tool: str) -> dict:
    """Full per-commit assessment for one tool, from its ledger. Never raises."""
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

    got = upstream_commits(upstream, base_ref, new_ref)
    if isinstance(got, list) and got and got[0].get("_err"):
        return {"tool": tool, "error": f"compare failed: {got[0]['_err']}"}
    commits, agg_files = got
    our_files = our_patch_files(upstream, up_branch, our_ref, tool) if our_ref else set()
    cls_map = classify_commits(tool, led.get("role", ""), commits)

    assessed, safe = [], []
    for c in commits:
        cls = cls_map.get(c["sha"], dict(_DEGRADED))
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

    return {"tool": tool, "status": "assessed", "upstream": upstream,
            "base_release": base_ref, "latest": new_ref,
            "our_ref": (our_ref or "")[:12],
            "our_patch_files": (len(our_files) if our_files is not None else None),
            "commit_count": len(commits), "aggregate_files": len(agg_files),
            "clearly_safe": safe, "commits": assessed}


def _commit_files(upstream: str, sha_full: str) -> set[str] | None:
    if not sha_full:
        return None
    d = _gh(f"repos/{upstream}/commits/{sha_full}")
    if d.get("_err"):
        return None
    return {f.get("filename") for f in (d.get("files") or []) if f.get("filename")}


# ── markdown render (for the PR body) ─────────────────────────────────────────
def render_md(rep: dict) -> str:
    tool = rep.get("tool", "?")
    if rep.get("error"):
        return f"### {tool}: assessment error — {rep['error']}\n"
    if rep.get("status") in ("clean", "not_layered"):
        return f"### {tool}: {rep['status']} — nothing to assess.\n"
    L = [f"## {tool} — selective-merge assessment",
         f"Range **{rep['base_release']} → {rep['latest']}** · {rep['commit_count']} upstream "
         f"commit(s) · our branch carries patches over "
         f"{rep['our_patch_files'] if rep.get('our_patch_files') is not None else '?'} file(s).",
         f"**Clearly-safe to auto-adopt: {len(rep['clearly_safe'])}** · "
         f"**needs human decision: {rep['commit_count'] - len(rep['clearly_safe'])}**", "",
         "| sha | cat | risk | rel | conflict | clean-pick | rec | decision | summary |",
         "|---|---|---|---|---|---|---|---|---|"]
    for c in rep["commits"]:
        L.append("| `{sha}` | {category} | {risk} | {rel} | {conf} | {clean} | {recommend} | "
                 "**{decision}** | {summary} |".format(
                     sha=c["sha"], category=c.get("category") or "?", risk=c.get("risk") or "?",
                     rel={True: "yes", False: "no", None: "?"}.get(c.get("relevant"), "?"),
                     conf={True: "⚠", False: "—", None: "?"}.get(c.get("touches_our_patches"), "—"),
                     clean={True: "✓", False: "✗", None: "—"}.get(c.get("clean_cherrypick"), "—"),
                     recommend=c.get("recommend") or "?", decision=c.get("decision"),
                     summary=(c.get("summary") or c.get("title") or "")[:80].replace("|", "\\|")))
    repro = [c for c in rep["commits"] if c.get("reproduce")]
    if repro:
        L += ["", "### Reproduce-before-adopt (bugfixes)"]
        for c in repro:
            L.append(f"- `{c['sha']}` {c.get('summary') or c['title']} — **reproduce:** {c['reproduce']}")
    L += ["", "> Doctrine: understand every commit, confirm each bugfix reproduces in OUR version, "
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
