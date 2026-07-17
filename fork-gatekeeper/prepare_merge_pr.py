#!/usr/bin/env python3
"""prepare_merge_pr.py — Phase 3: open a cherry-pick MERGE PR for human review.

For each behind fork, take the commits the assessment marked `clearly_safe` (the LLM judged
them USEFUL *and* the deterministic gate confirmed clean_cherrypick + no overlap with our
carried patches), cherry-pick them onto a fresh candidate branch off our maintained vibeic
branch, push that branch, and open a PR on the fork proposing to merge it — FOR HUMAN REVIEW.

Safety (the point of all the prior reviews):
  * This is DETERMINISTIC code, not an agentic LLM. It holds the git token; the LLM never did.
  * It only cherry-picks the SPECIFIC upstream shas the assessment already vetted; the PR diff
    is the real upstream commits, fully reviewable. A poisoned LLM verdict can at most propose
    a real (reviewable) upstream commit — the human catches it on the PR.
  * NEVER force-pushes; NEVER touches main or the vibeic branch directly — it pushes only a NEW
    `gk-merge/<date>` candidate branch and opens a PR (base = the vibeic branch). Never auto-merges.
  * A cherry-pick conflict → abort that tool, leave every tree clean, report → human. Never raises.
  * Gated: gatekeeper only calls this when GK_MERGE_PR is enabled (opt-in, off by default).
  * GK_PR_DRYRUN → does the cherry-pick in a throwaway worktree but pushes/opens nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
LEDGER = STATE / "ledger"
FORKS_DIR = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")


def _run(args, cwd=None, timeout=180):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{e.__class__.__name__}: {e}"


def _fork_remote(clone: Path, tool: str) -> str | None:
    """The remote whose URL is the vibeic fork (add it if missing). None on failure."""
    url = f"https://github.com/vibeic/{tool}.git"
    rc, out = _run(["git", "-C", str(clone), "remote", "-v"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == url and "(fetch)" in line:
            return parts[0]
    _run(["git", "-C", str(clone), "remote", "remove", "gk_fork"])
    rc, _ = _run(["git", "-C", str(clone), "remote", "add", "gk_fork", url])
    return "gk_fork" if rc == 0 else None


def _prepare_one(tool: str, rep: dict, date: str) -> dict:
    dry = os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes")
    safe_short = rep.get("clearly_safe") or []
    if not safe_short:
        return {"tool": tool, "status": "nothing_safe"}
    # map short shas → full shas + titles from the assessment
    by_short = {c["sha"]: c for c in rep.get("commits") or []}
    picks = [by_short[s] for s in safe_short if s in by_short and by_short[s].get("sha_full")]
    if not picks:
        return {"tool": tool, "status": "no_full_shas"}

    led_p = LEDGER / f"{tool}.json"
    if not led_p.is_file():
        return {"tool": tool, "status": "no_ledger"}
    try:
        led = json.loads(led_p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"tool": tool, "status": "bad_ledger"}
    branch = led.get("vibeic_branch")
    upstream = led.get("upstream") or rep.get("upstream")
    if not branch:
        return {"tool": tool, "status": "no_vibeic_branch",
                "note": "add a '# … branch <name>' comment to the ARG to enable merge PRs"}
    clone = FORKS_DIR / tool
    if not (clone / ".git").is_dir():
        return {"tool": tool, "status": "no_clone"}

    fr = _fork_remote(clone, tool)
    if not fr:
        return {"tool": tool, "status": "no_fork_remote"}
    up_url = f"https://github.com/{upstream}.git"
    up = next((l.split()[0] for l in _run(["git", "-C", str(clone), "remote", "-v"])[1].splitlines()
               if len(l.split()) >= 2 and l.split()[1] == up_url and "(fetch)" in l), None)
    if not up:
        _run(["git", "-C", str(clone), "remote", "remove", "gk_up"])
        _run(["git", "-C", str(clone), "remote", "add", "gk_up", up_url]); up = "gk_up"
    # a SILENT fork-fetch failure would leave a STALE remote-tracking base → a misleading
    # (redundant) PR; bail explicitly so a stale base can never quietly produce one.
    if _run(["git", "-C", str(clone), "fetch", fr, "-q"], timeout=300)[0] != 0:
        return {"tool": tool, "status": "fetch_failed", "note": "fork fetch failed — retry next tick"}
    _run(["git", "-C", str(clone), "fetch", up, "--tags", "-q"], timeout=300)

    base_ref = f"refs/remotes/{fr}/{branch}"
    if _run(["git", "-C", str(clone), "rev-parse", "--verify", base_ref])[0] != 0:
        return {"tool": tool, "status": "no_branch_ref", "note": f"{fr}/{branch} not found"}

    # UNIQUE worktree/body paths (uuid) so a concurrent hand-run can't rm -rf a live worktree.
    wt = Path(tempfile.gettempdir()) / f"gk-merge-{tool}-{uuid.uuid4().hex[:10]}"
    bf = None
    if _run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(wt), base_ref])[0] != 0:
        return {"tool": tool, "status": "worktree_fail"}
    try:
        applied = []
        for c in picks:
            rc, out = _run(["git", "-C", str(wt), "cherry-pick", c["sha_full"]])
            if rc != 0:
                _run(["git", "-C", str(wt), "cherry-pick", "--abort"])
                return {"tool": tool, "status": "conflict",
                        "note": f"{c['sha']} did not apply cleanly onto {branch} — deferred to human",
                        "applied_before_conflict": [a["sha"] for a in applied]}
            applied.append(c)
        cand = f"gk-merge/{date}"
        body_lines = [f"Auto-prepared merge of {len(applied)} upstream commit(s) the LLM judged "
                      f"useful for our **{tool}** fork and that cherry-pick cleanly onto "
                      f"`{branch}` with no conflict with our carried patches. **Review the diffs "
                      f"and merge (or close).**", ""]
        for c in applied:
            body_lines.append(f"- `{c['sha']}` {c.get('title','')} — {c.get('summary','')}")
        body_lines += ["", "_Opened by the eda-fork-gatekeeper. Real upstream commits, "
                       "human-reviewed; NOT an auto-merge._"]
        body = "\n".join(body_lines)

        if dry:
            rc, log = _run(["git", "-C", str(wt), "log", "--oneline", f"{base_ref}..HEAD"])
            return {"tool": tool, "status": "would_open", "branch": cand, "base": branch,
                    "applied": [a["sha"] for a in applied], "preview": log.strip()[:400]}

        # dupe-guard: only STDOUT means "exists"; an ls-remote FAILURE must NOT be misread as
        # "exists" (that would silently drop the merge) — retry next tick instead.
        rcl, outl = _run(["git", "-C", str(clone), "ls-remote", "--heads", fr, cand])
        if rcl != 0:
            return {"tool": tool, "status": "lsremote_failed", "note": "retry next tick"}
        if outl.strip():
            return {"tool": tool, "status": "already_exists", "branch": cand}
        # push ONLY the new candidate branch (never force, never the vibeic branch/main)
        rc, out = _run(["git", "-C", str(wt), "push", fr, f"HEAD:refs/heads/{cand}", "-q"])
        if rc != 0:
            return {"tool": tool, "status": "push_failed", "note": out.strip()[:200]}
        bf = Path(tempfile.gettempdir()) / f"gk-merge-body-{tool}-{uuid.uuid4().hex[:8]}.md"
        bf.write_text(body)
        title = f"[eda-fork] {tool}: merge {len(applied)} useful upstream commit(s) → {branch}"
        rc, out = _run(["gh", "pr", "create", "-R", f"vibeic/{tool}", "--base", branch,
                        "--head", cand, "--title", title, "--body-file", str(bf)])
        if rc != 0:
            # PR failed but the branch is pushed → delete it so a retry re-pushes + re-PRs
            # (else the dupe-guard would skip it forever, orphaning the branch).
            _run(["git", "-C", str(clone), "push", fr, "--delete", cand, "-q"])
            return {"tool": tool, "status": "pr_failed", "note": out.strip()[:200]}
        url = out.strip().splitlines()[-1] if out.strip() else "(created)"
        return {"tool": tool, "status": "opened", "url": url, "branch": cand,
                "applied": [a["sha"] for a in applied]}
    finally:
        _run(["git", "-C", str(clone), "worktree", "remove", "--force", str(wt)])
        _run(["rm", "-rf", str(wt)])
        if bf is not None:
            try:
                bf.unlink()
            except OSError:
                pass


def prepare(assessments: dict, date: str) -> list[dict]:
    """Open a cherry-pick merge PR per behind fork that has a clearly-safe set. Never raises."""
    out = []
    for tool, rep in (assessments or {}).items():
        if not isinstance(rep, dict) or rep.get("status") != "assessed":
            continue
        try:
            out.append(_prepare_one(tool, rep, date))
        except Exception as e:  # noqa: BLE001 — a merge-PR hiccup must never break the tick
            out.append({"tool": tool, "status": "error", "note": str(e)[:200]})
    return out
