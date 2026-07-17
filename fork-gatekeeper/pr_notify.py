#!/usr/bin/env python3
"""pr_notify.py — open a PR on vibe-ic recording an actionable fork-gatekeeper tick.

Replaces email notification (owner directive: "use a PR on vibe-ic to handle all forked
OSS EDA tools"). One PR per actionable day — a MERGED promote, or a new upstream release
that FAILED to integrate. The PR is BOTH the human-facing surface AND a real change:

  * MERGED   → bump the `vibeic-eda:<old> → <new>` image pins in vibe-ic's user docs
               (README.md, docs/INSTALL.md — they otherwise silently drift), and append a
               dated row to the machine-owned tools/vibeic-eda/EDA_FORK_SYNC_LOG.md.
  * DEFERRED (a new release that could not integrate) → append a "needs manual rebase"
               backlog row to that same log (issue-via-PR, per "USE PR to issue bugs").

Left for human / repo-gatekeeper review — NEVER auto-merged. Uses a THROWAWAY git worktree
off origin/main, so it never touches the clone's own working tree (which carries untracked
benchmark-data). NEVER raises — a PR hiccup must not break the daily tick. Requires `gh`
authenticated with repo scope on the vibeic org.

    open_pr(summary, report_md) -> (ok, detail)

GK_PR_DRYRUN=1 does every local step (worktree, edits, commit) but skips the push + gh, and
leaves the diff visible for inspection.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("GK_VIBEIC_REPO", "/home/reyerchu/vibe-ic"))
GH_REPO = "vibeic/vibe-ic"
DOC_FILES = ["README.md", "docs/INSTALL.md"]           # where vibe-ic pins the image tag
LOG_FILE = "tools/vibeic-eda/EDA_FORK_SYNC_LOG.md"      # machine-owned append-only record
_PIN_RE = re.compile(r"(vibeic-eda:)\d+\.\d+\.\d+")


def _run(args, cwd=None):
    """Run a command; return (rc, combined_output). Never raises."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{e.__class__.__name__}: {e}"


def _actionable(summary):
    """(merged, failed) — merged tools, and DEFERRED tools that have a NEW release (a real
    integration failure, not merely un-layered/clean)."""
    merged, failed = [], []
    for r in summary.get("results", []):
        if r.get("verdict") == "MERGED":
            merged.append(r)
        elif r.get("verdict") == "DEFERRED" and (r.get("new_releases") or 0) > 0:
            failed.append(r)
    return merged, failed


def _log_entry(summary, merged, failed) -> str:
    ver = summary.get("image_version") or "?"
    lines = [f"## {summary.get('date','?')} — vibeic-eda:{ver}", ""]
    for r in merged:
        lines.append(f"- **MERGED** {r['tool']} → {r.get('latest_release','?')} — {r.get('note','')}")
    for r in failed:
        lines.append(f"- **DEFERRED** {r['tool']} → {r.get('latest_release','?')} — {r.get('note','')}")
    return "\n".join(lines) + "\n\n"


def open_pr(summary, report_md) -> tuple[bool, str]:
    merged, failed = _actionable(summary)
    if not (merged or failed):
        return (False, "nothing actionable — no PR")
    if not REPO.is_dir():
        return (False, f"vibe-ic clone not found at {REPO}")

    date = str(summary.get("date", "")).strip() or "undated"
    branch = f"eda-fork-sync-{date}"
    dry = os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes")

    rc, out = _run(["git", "-C", str(REPO), "fetch", "origin", "main", "-q"])
    if rc != 0:
        return (False, f"git fetch origin failed: {out.strip()[:200]}")
    # a same-day PR branch already on origin → a PR is already open for today; don't dupe
    rc, out = _run(["git", "-C", str(REPO), "ls-remote", "--heads", "origin", branch])
    if rc == 0 and out.strip() and not dry:
        return (False, f"PR branch {branch} already exists on origin — skipping duplicate")

    wt = Path(tempfile.gettempdir()) / f"gk-vibeic-pr-{date}"
    _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
    shutil.rmtree(wt, ignore_errors=True)
    rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch, str(wt), "origin/main"])
    if rc != 0:
        # branch may linger locally from a killed run — retry detached then branch
        _run(["git", "-C", str(REPO), "branch", "-D", branch])
        rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch, str(wt), "origin/main"])
        if rc != 0:
            return (False, f"worktree add failed: {out.strip()[:200]}")

    try:
        changed = []
        # MERGED → bump the image-version pins in the user docs to the shipped version
        newver = summary.get("image_version")
        if merged and newver:
            for rel in DOC_FILES:
                f = wt / rel
                if not f.is_file():
                    continue
                txt = f.read_text()
                bumped = _PIN_RE.sub(rf"\g<1>{newver}", txt)
                if bumped != txt:
                    f.write_text(bumped)
                    changed.append(rel)
        # always append the sync-log record (create with a header if absent)
        logf = wt / LOG_FILE
        entry = _log_entry(summary, merged, failed)
        if logf.is_file():
            logf.write_text(logf.read_text().rstrip("\n") + "\n\n" + entry)
        else:
            logf.parent.mkdir(parents=True, exist_ok=True)
            logf.write_text(
                "# EDA Fork Sync Log\n\n"
                "Machine-owned, append-only. One entry per actionable fork-gatekeeper tick.\n"
                "MERGED = a fork release integrated + shipped in a new vibeic-eda image.\n"
                "DEFERRED = a new upstream release that failed to integrate (needs a human).\n\n"
                + entry)
        changed.append(LOG_FILE)

        _run(["git", "-C", str(wt), "add", *changed])
        title = (f"[eda-fork] {date}: MERGED {len(merged)} · DEFERRED {len(failed)}"
                 + (f" — vibeic-eda:{newver}" if merged and newver else ""))
        rc, out = _run(["git", "-C", str(wt), "commit", "-q", "-m", title])
        if rc != 0:
            return (False, f"commit failed: {out.strip()[:200]}")

        body = (report_md or "").rstrip() + (
            "\n\n---\n_Opened automatically by the eda-fork-gatekeeper. "
            "MERGED rows bump the `vibeic-eda:` doc pins to the shipped image; DEFERRED rows "
            "are a backlog item (a new upstream release that needs a manual rebase). "
            "Review + merge (or close) — this PR is the record, not an auto-merge._\n")

        if dry:
            rc, diff = _run(["git", "-C", str(wt), "show", "--stat", "HEAD"])
            return (True, f"DRY-RUN — would open PR '{title}' on {GH_REPO}\n{diff.strip()[:800]}")

        rc, out = _run(["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:{branch}"])
        if rc != 0:
            return (False, f"branch push failed: {out.strip()[:200]}")
        bf = Path(tempfile.gettempdir()) / f"gk-pr-body-{date}.md"
        bf.write_text(body)
        rc, out = _run(["gh", "pr", "create", "-R", GH_REPO, "--base", "main",
                        "--head", branch, "--title", title, "--body-file", str(bf)])
        if rc != 0:
            return (False, f"gh pr create failed: {out.strip()[:200]}")
        url = out.strip().splitlines()[-1] if out.strip() else "(created)"
        return (True, f"opened PR: {url}")
    finally:
        _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(wt, ignore_errors=True)
        _run(["git", "-C", str(REPO), "branch", "-D", branch])   # local branch not needed (origin has it)
