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
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _nda_tokens import find as _nda_find      # noqa: E402
# HARD import. The verdict tallies this module puts in a PR TITLE are the same ones
# the report body states, and they come from one place (vibe-ic#875). A soft import
# with a private fallback would restore exactly the second arithmetic that put two
# different numbers under one word.
import report_counts                           # noqa: E402

try:
    # The ONE derivation of the headline counts (vibeic/vibeic-eda#7). Imported, never
    # re-implemented: this module publishing its own arithmetic is what put a third
    # number in front of the reviewer who reads the PR body first.
    import assess_release as _assess           # noqa: E402
except Exception:  # noqa: BLE001
    _assess = None

REPO = Path(os.environ.get("GK_VIBEIC_REPO", "/home/reyerchu/vibe-ic"))
GH_REPO = "vibeic/vibe-ic"
DOC_FILES = ["README.md", "docs/INSTALL.md"]           # where vibe-ic pins the image tag
LOG_FILE = "tools/vibeic-eda/EDA_FORK_SYNC_LOG.md"      # machine-owned append-only record
#: vibe-ic's OWN anchor tool. It owns the pointer list (15 ghcr pointers across 9
#: files, plus 24 install-doc refs); `DOC_FILES` above is a second, shorter list
#: that covers 2 of them. Anything bumping the anchor must call this rather than
#: re-derive where the version is written — see `open_anchor_pr`.
ANCHOR_TOOL = "tools/vibeic-eda/sync_image_version.py"
ASSESS_DIR = "tools/vibeic-eda/upstream-assessments"   # per-tick selective-merge assessments
_PIN_RE = re.compile(r"(vibeic-eda:)\d+\.\d+\.\d+")



def _nda_block_push(wt, rev_range: str = "origin/main..HEAD") -> str:
    """"" if the range is clean, else a REFUSAL message.

    vibe-ic#395: these push sites write branches to PUBLIC fork repositories
    and scanned nothing. That is a wider exposure than the vibe-ic CI diffs
    (which gained this scan in v1.6.27/28), on the exact path where this
    project already paid for a leak twice in force-pushed history rewrites.

    Scans the commit MESSAGES, the added CONTENT and the added PATHS — all
    three carriers — and reports token INDICES only, never the literal.
    A range that cannot be resolved is scanned as the tip commit rather than
    reported clean: unknown must not read as safe.
    """
    import subprocess as _sp
    try:
        r = _sp.run(["git", "-C", str(wt), "log", "--format=%B", rev_range],
                    capture_output=True, text=True, timeout=120)
        msgs = r.stdout if r.returncode == 0 else ""
        d = _sp.run(["git", "-C", str(wt), "diff", "--unified=0", rev_range],
                    capture_output=True, text=True, timeout=300)
        diff = d.stdout if d.returncode == 0 else ""
        if r.returncode != 0 or d.returncode != 0:
            r2 = _sp.run(["git", "-C", str(wt), "show", "--format=%B", "HEAD"],
                         capture_output=True, text=True, timeout=300)
            msgs, diff = r2.stdout, r2.stdout
    except Exception as exc:  # noqa: BLE001 — a guard that dies must not pass
        return f"NDA pre-push scan could not run ({exc.__class__.__name__}) — refusing to push"
    added = "\n".join(l for l in diff.splitlines()
                       if l.startswith("+") and not l.startswith("+++"))
    paths = "\n".join(l for l in diff.splitlines() if l.startswith("+++ b/"))
    hits = sorted(set(_nda_find(msgs)) | set(_nda_find(added)) | set(_nda_find(paths)))
    if hits:
        return (f"NDA pre-push guard: {len(hits)} token role(s) present "
                f"(indices {hits}) in the commit messages / added content / "
                f"added paths of {rev_range} — REFUSING to push to a public "
                f"repository. Literals are not printed by design (vibe-ic#395).")
    return ""

def _run(args, cwd=None):
    """Run a command; return (rc, combined_output). Never raises."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{e.__class__.__name__}: {e}"


def _actionable(summary):
    """(merged, failed) — merged tools, and DEFERRED tools that have a NEW release (a real
    integration failure, not merely un-layered/clean).

    A tool whose release gap is UNKNOWN is actionable too. `new_releases` is null
    whenever containment could not be decided for some upstream release, and
    `(r.get("new_releases") or 0) > 0` reads null as zero — which would drop the
    one row where nobody knows what we are missing, on the grounds that we are
    missing nothing.

    …and since vibeic-eda#101 that row's VERDICT is UNMEASURABLE, not DEFERRED.
    Matching on the verdict string alone would have quietly un-escalated exactly
    the rows the paragraph above was written to keep — the same defect one level
    over, arrived at by renaming the thing being matched. The `unknown` arm still
    does the selecting, so a NOT-PROBED row (an upstream that publishes no release
    at all) stays out: it is unmeasurable and it is also not actionable, and a PR
    every morning about a state no human can clear is how a channel gets muted.
    """
    merged, failed = [], []
    for r in summary.get("results", []):
        if r.get("verdict") == "MERGED":
            merged.append(r)
        elif (r.get("verdict") in ("DEFERRED", "UNMEASURABLE")
                and ((isinstance(r.get("new_releases"), int) and r["new_releases"] > 0)
                     or r.get("new_releases_status") == "unknown")):
            failed.append(r)
    return merged, failed


def _log_entry(summary, merged, failed) -> str:
    ver = summary.get("image_version") or "?"
    lines = [f"## {summary.get('date','?')} — vibeic-eda:{ver}", ""]
    for r in merged:
        lines.append(f"- **MERGED** {r['tool']} → {r.get('latest_release','?')} — {r.get('note','')}")
    for r in failed:
        # The ROW'S OWN verdict, not the literal this list was named after
        # (vibeic-eda#101). `failed` now carries UNMEASURABLE rows too, and
        # stamping DEFERRED on them here would republish the collapse the new
        # verdict exists to end — in the one document a human actually reads.
        lines.append(f"- **{r.get('verdict') or 'DEFERRED'}** {r['tool']} → "
                     f"{r.get('latest_release') or '?'} — {r.get('note','')}")
    return "\n".join(lines) + "\n\n"


def open_pr(summary, report_md) -> tuple[bool, str]:
    merged, failed = _actionable(summary)
    if not (merged or failed):
        return (False, "nothing actionable — no PR")
    if not REPO.is_dir():
        return (False, f"vibe-ic clone not found at {REPO}")

    # THE TITLE AND THE BODY STATE ONE SET OF NUMBERS (vibe-ic#875, #838).
    # This used to build "DEFERRED {len(failed)}" — the ACTIONABLE subset — and
    # bolt it onto a body that had independently rendered "DEFERRED
    # {counts['DEFERRED']}" over a table with that many rows. Both were right
    # about their own population and the PR contradicted itself, twice.
    #
    # Now the tallies come from the one derivation the report itself used, and
    # the actionable subset keeps its own NAME below rather than borrowing the
    # word DEFERRED. Refuse before touching git: a PR whose headline cannot be
    # stated is not a PR to open with a guessed one.
    try:
        counts = report_counts.verdict_counts(summary)
    except report_counts.CountsUnavailable as e:
        return (False, f"refusing to title a PR whose counts cannot be stated: {e}")
    # …and the check that this is true of the BYTES, not of two call sites that
    # happen to agree today: read the headline back out of the exact body about
    # to be published. Unreadable is its own outcome, never "close enough".
    body_counts = report_counts.parse_phrase(report_md or "")
    if body_counts is None:
        return (False, "refusing: the report body states no headline counts, so the "
                       "title cannot be shown to agree with the body it publishes")
    if body_counts != counts:
        return (False, f"refusing: the title would state {counts} while the body it "
                       f"publishes states {body_counts}")

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
                "DEFERRED = a new upstream release that failed to integrate (needs a human).\n"
                "UNMEASURABLE = the question was not answered — not a deferral and not a "
                "clean bill (vibeic-eda#101).\n\n"
                + entry)
        changed.append(LOG_FILE)

        _run(["git", "-C", str(wt), "add", *changed])
        # `phrase` narrows to the verdicts a title has room for; it cannot change
        # what any number IS — every one still comes out of `counts`, the same
        # dict the body's headline was rendered from and checked against.
        #
        # UNMEASURABLE is NAMED here rather than folded into DEFERRED
        # (vibeic-eda#101): a title that says "DEFERRED 10" over seven rows whose
        # release question was never answered states a triage result for rows that
        # have none. It is stated only when it occurred, so a day with none reads
        # exactly as it did before. Both fixes are the same rule — the number and
        # the word it follows have to describe one population.
        named = ("MERGED", "DEFERRED") + (
            ("UNMEASURABLE",) if counts.get("UNMEASURABLE") else ())
        head = report_counts.phrase(counts, named)
        # The actionable subset, under its OWN name. Stated only when it differs,
        # because "(10 actionable)" beside "DEFERRED 10" is noise; but when it
        # differs it is the whole reason this PR exists, and dropping it would
        # trade a contradiction for a silence. Measured against the verdicts the
        # title just named, since `failed` now carries UNMEASURABLE rows too.
        if len(failed) != sum(counts[v] for v in named if v != "MERGED"):
            head += f" ({len(failed)} actionable)"
        title = (f"[eda-fork] {date}: {head}"
                 + (f" — vibeic-eda:{newver}" if merged and newver else ""))
        rc, out = _run(["git", "-C", str(wt), "commit", "-q", "-m", title])
        if rc != 0:
            return (False, f"commit failed: {out.strip()[:200]}")

        body = (report_md or "").rstrip() + (
            "\n\n---\n_Opened automatically by the eda-fork-gatekeeper. "
            "MERGED rows bump the `vibeic-eda:` doc pins to the shipped image. "
            f"{len(failed)} of the {counts['DEFERRED']} DEFERRED row(s) are ACTIONABLE — a "
            "measured new upstream release, or a release gap that could not be measured at "
            "all — and those are the backlog item (a manual rebase). The remaining DEFERRED "
            "rows are deferred for a reason that is not a pending release: there is no new "
            "upstream work for anyone to integrate. "
            "Review + merge (or close) — this PR is the record, not an auto-merge._\n")

        if dry:
            rc, diff = _run(["git", "-C", str(wt), "show", "--stat", "HEAD"])
            return (True, f"DRY-RUN — would open PR '{title}' on {GH_REPO}\n{diff.strip()[:800]}")

        _nda_stop = _nda_block_push(wt)
        if _nda_stop:
            return (False, _nda_stop)
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


def open_anchor_pr(version: str) -> tuple[bool, str]:
    """Open a vibe-ic PR advancing the eda image anchor to `version`.

    WHY THIS EXISTS (vibe-ic#754). Publishing an image and advancing vibe-ic's
    anchor were two manual actions with nothing linking them, so every release
    moved `:latest` off the pinned version BY CONSTRUCTION and the two were
    reunited only because a landing gate happened to look. That repair was
    applied by hand four times — 0.2.62 ("the gate caught it for the third
    time") and 0.2.63 among them. A repair that keeps being applied by hand is a
    missing step, not a recurring accident.

    Between the publish and the next landing, anyone pulling the tag that means
    "newest" gets a different toolchain from the one vibe-ic pins, and their
    results are not comparable (vibe-ic#423).

    IT DELEGATES TO VIBE-IC'S OWN TOOL. `open_pr` bumps `DOC_FILES` with a
    regex — 2 files of the 9 that carry a pointer — which is why a tick that DID
    fire still left the anchor failing its own gate. Re-deriving "where the
    version is written" in this repo is how the second list drifts from the
    first; `sync_image_version.py --set` is the one place that knows, and it
    verifies itself afterwards.
    """
    version = (version or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return (False, f"refusing to anchor a malformed version: {version!r}")
    if not REPO.is_dir():
        return (False, f"vibe-ic clone not found at {REPO}")

    branch = f"eda-anchor-{version}"
    dry = os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes")

    rc, out = _run(["git", "-C", str(REPO), "fetch", "origin", "main", "-q"])
    if rc != 0:
        return (False, f"git fetch origin failed: {out.strip()[:200]}")
    rc, out = _run(["git", "-C", str(REPO), "ls-remote", "--heads", "origin", branch])
    if rc == 0 and out.strip() and not dry:
        return (False, f"branch {branch} already on origin — anchor PR already open")

    wt = Path(tempfile.gettempdir()) / f"gk-vibeic-anchor-{version}"
    _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
    shutil.rmtree(wt, ignore_errors=True)
    rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch,
                    str(wt), "origin/main"])
    if rc != 0:
        _run(["git", "-C", str(REPO), "branch", "-D", branch])
        rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch,
                        str(wt), "origin/main"])
        if rc != 0:
            return (False, f"worktree add failed: {out.strip()[:200]}")
    try:
        tool = wt / ANCHOR_TOOL
        if not tool.is_file():
            return (False, f"{ANCHOR_TOOL} not present in vibe-ic — cannot anchor")
        rc, out = _run(["python3", str(tool), "--set", version], cwd=str(wt))
        if rc != 0:
            return (False, f"anchor tool refused {version}: {out.strip()[-300:]}")
        rc, dirty = _run(["git", "-C", str(wt), "status", "--porcelain"])
        if not [l for l in dirty.splitlines() if l and not l.startswith("??")]:
            return (False, f"anchor already at {version} — nothing to open")

        _run(["git", "-C", str(wt), "add", "-u"])
        title = f"chore(image-anchor): adopt vibeic-eda {version}"
        rc, out = _run(["git", "-C", str(wt), "commit", "-q", "-m", title])
        if rc != 0:
            return (False, f"commit failed: {out.strip()[:200]}")

        body = (
            f"`vibeic-eda:{version}` is published, so `:latest` no longer resolves to "
            f"the version this repo anchors. Opened by the release itself rather than "
            f"waiting for a landing gate to notice.\n\n"
            f"Produced by running vibe-ic's own `{ANCHOR_TOOL} --set {version}` — the "
            f"tool that owns the pointer list — not by re-deriving where the version is "
            f"written.\n\n"
            f"Closes the hand step described in vibe-ic#754.\n")
        if dry:
            rc, diff = _run(["git", "-C", str(wt), "show", "--stat", "HEAD"])
            return (True, f"DRY-RUN — would open '{title}'\n{diff.strip()[:800]}")

        stop = _nda_block_push(wt)
        if stop:
            return (False, stop)
        rc, out = _run(["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:{branch}"])
        if rc != 0:
            return (False, f"branch push failed: {out.strip()[:200]}")
        bf = Path(tempfile.gettempdir()) / f"gk-anchor-body-{version}.md"
        bf.write_text(body)
        rc, out = _run(["gh", "pr", "create", "-R", GH_REPO, "--base", "main",
                        "--head", branch, "--title", title, "--body-file", str(bf)])
        if rc != 0:
            return (False, f"gh pr create failed: {out.strip()[:200]}")
        return (True, f"opened anchor PR: {out.strip().splitlines()[-1] if out.strip() else '(created)'}")
    finally:
        _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(wt, ignore_errors=True)
        _run(["git", "-C", str(REPO), "branch", "-D", branch])


def tally_line(tool: str, a: dict) -> str | None:
    """One fork's line in the review PR's body. None if the counts cannot be derived.

    Pure, so the numbers a reviewer reads FIRST can be tested without a git worktree or
    a GitHub call — which is why they were not, and how this line kept its own arithmetic
    for as long as it did.
    """
    if a.get("error"):
        return (f"- **{tool}**: could not enumerate the new release — {a['error']} "
                f"(needs manual review)")
    # vibeic/vibeic-eda#7: this line used to compute `commit_count - clearly_safe`
    # UNCONDITIONALLY — it never read `outstanding` at all, not even as a fallback. On
    # 2026-07-28 that put "108 need review" in the PR body of the very tick whose
    # attached table said 105 and whose daily report said 105; re-run on the repaired
    # assessment it would have said 107 against their 2. The PR body is the first and
    # often only thing a reviewer reads, so it was the copy most likely to be believed
    # and the only one nothing derived from the assessment.
    if _assess is None:
        return None
    if a.get("status") == "pin_ahead_of_release":
        d = a.get("target_direction") or {}
        return (f"- **{tool}**: NO TARGET — the newest tagged release `{d.get('target')}` "
                f"is not a descendant of the ref we ship (`{d.get('pin')}`): "
                f"{d.get('why')}. Nothing is proposed (needs manual review)")
    n = _assess.summary_counts(a)
    line = (f"- **{tool}**: {n['commits']} upstream commit(s) {a.get('base_release')} → "
            f"{a.get('latest')} — {n['clearly_safe']} clearly-safe, {n['carried']} already "
            f"carried, {n['decided']} previously decided, {n['outstanding']} "
            f"need human review{_assess.direction_note(a)}")
    # The PR body is the first and often only thing a reviewer reads, so the fact that
    # our own mainline already merged some of these — the 2026-08-06 tick's PR listed 42
    # such commits out of 54 as "need human review" — belongs HERE, not only in the
    # attached table. Same wording as the assessment file and the daily report.
    line += _assess.mainline_clause(n, a)
    # An incomplete judgment must be visible in the PR BODY, not only in the attached
    # per-commit table. Otherwise the summary a reviewer reads first presents "N need
    # review" as a triage result when nothing was triaged.
    if n["not_assessed"]:
        line += (f" — ⚠ **the judge did not complete: {n['not_assessed']} commit(s) NOT "
                 f"ASSESSED** (no classification was made for them; this is missing "
                 f"analysis, not a risk finding)")
    # vibeic/vibeic-eda#5. A commit the model called relevant that nothing we run can
    # reach is the disagreement worth surfacing FIRST — it is the one whose stated
    # justification a reviewer would otherwise take at face value.
    if n["unreachable"]:
        line += (f" — ⚠ **{n['unreachable']} UNREACHABLE from our command surface** (the "
                 f"judge called them relevant; the deterministic check says nothing our "
                 f"emitters issue reaches the symbols they change — human decision, "
                 f"not auto-proposed)")
    # vibeic/vibeic-eda#6. A verdict only one sample supports is the other kind of claim
    # a reviewer would otherwise take at face value: the row reads like a judgement, and
    # the same input judged again returns something else.
    if n["unconfirmed"]:
        line += (f" — ⚠ **{n['unconfirmed']} JUDGEMENT(S) DID NOT REPRODUCE** (they cleared "
                 f"every other auto-adopt condition, so the same commit text was "
                 f"re-judged by independent samples and the readings differed, or one "
                 f"never arrived — every reading is printed on the row, none averaged "
                 f"into a majority; human decision, not auto-proposed)")
    return line


def open_assessment_pr(summary, assessments, rendered) -> tuple[bool, str]:
    """Open a vibe-ic REVIEW PR carrying the selective-merge assessments for the behind
    forks: one markdown per tool under tools/vibeic-eda/upstream-assessments/, plus a body
    that tallies, per tool, how many upstream commits are clearly-safe vs need a human
    decision. This is a review request (adopt selectively), NOT an auto-merge. Same
    worktree-isolated, dupe-guarded, never-raises discipline as open_pr."""
    # include forks with commits AND forks whose enumeration ERRORED (a new release we
    # couldn't read still needs surfacing — it must never silently vanish from every PR).
    tools = sorted(t for t, a in (assessments or {}).items()
                   if (a.get("commit_count") or 0) > 0 or a.get("error"))
    if not tools:
        return (False, "no assessments to file — no PR")
    if not REPO.is_dir():
        return (False, f"vibe-ic clone not found at {REPO}")

    date = str(summary.get("date", "")).strip() or "undated"
    branch = f"eda-fork-assess-{date}"
    dry = os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes")

    rc, out = _run(["git", "-C", str(REPO), "fetch", "origin", "main", "-q"])
    if rc != 0:
        return (False, f"git fetch origin failed: {out.strip()[:200]}")
    rc, out = _run(["git", "-C", str(REPO), "ls-remote", "--heads", "origin", branch])
    if rc == 0 and out.strip() and not dry:
        return (False, f"assessment branch {branch} already exists on origin — skipping duplicate")

    wt = Path(tempfile.gettempdir()) / f"gk-vibeic-assess-{date}"
    _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
    shutil.rmtree(wt, ignore_errors=True)
    rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch, str(wt), "origin/main"])
    if rc != 0:
        _run(["git", "-C", str(REPO), "branch", "-D", branch])
        rc, out = _run(["git", "-C", str(REPO), "worktree", "add", "-q", "-b", branch, str(wt), "origin/main"])
        if rc != 0:
            return (False, f"worktree add failed: {out.strip()[:200]}")

    try:
        adir = wt / ASSESS_DIR
        adir.mkdir(parents=True, exist_ok=True)
        tally = []
        for t in tools:
            md = rendered.get(t) or f"## {t}\n(no render)\n"
            (adir / f"{date}-{t}.md").write_text(md)
            line = tally_line(t, assessments[t])
            if line is None:
                return (False, "assess_release is not importable — refusing to publish a "
                               "tally this module would have to derive for itself")
            # The PR body and the table it links are one publication and must not be able
            # to leave here disagreeing. `rendered` and `assessments` arrive as two
            # separately-built maps, so this also catches a caller that pairs a table with
            # a DIFFERENT report — the shape of the 2026-07-28 divergence.
            bad = _assess.cross_check(assessments[t], {"pr": line, "assessment": md})
            if bad:
                return (False, "refusing to open a PR whose body and attached assessment "
                               "disagree: " + "; ".join(bad[:3]))
            tally.append(line)
        _run(["git", "-C", str(wt), "add", ASSESS_DIR])
        title = f"[eda-fork] {date}: upstream release assessment — {len(tools)} tool(s) to review"
        rc, out = _run(["git", "-C", str(wt), "commit", "-q", "-m", title])
        if rc != 0:
            return (False, f"commit failed: {out.strip()[:200]}")

        body = ("## Selective-merge assessment — human review requested\n\n"
                "New upstream release(s) detected. Per the selective-merge doctrine we do NOT "
                "blindly rebase; below is a per-commit triage. Adopt the clearly-safe subset, and "
                "for each other commit decide adopt/skip — confirming any bugfix reproduces in our "
                "version first.\n\n" + "\n".join(tally) +
                "\n\nFull per-commit tables are in `" + ASSESS_DIR + "/`.\n\n---\n"
                "_Opened automatically by the eda-fork-gatekeeper. Review request, not an "
                "auto-merge._\n")

        if dry:
            rc, diff = _run(["git", "-C", str(wt), "show", "--stat", "HEAD"])
            return (True, f"DRY-RUN — would open assessment PR '{title}'\n{diff.strip()[:800]}")

        _nda_stop = _nda_block_push(wt)
        if _nda_stop:
            return (False, _nda_stop)
        rc, out = _run(["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:{branch}"])
        if rc != 0:
            return (False, f"branch push failed: {out.strip()[:200]}")
        bf = Path(tempfile.gettempdir()) / f"gk-assess-body-{date}.md"
        bf.write_text(body)
        rc, out = _run(["gh", "pr", "create", "-R", GH_REPO, "--base", "main",
                        "--head", branch, "--title", title, "--body-file", str(bf)])
        if rc != 0:
            return (False, f"gh pr create failed: {out.strip()[:200]}")
        url = out.strip().splitlines()[-1] if out.strip() else "(created)"
        return (True, f"opened assessment PR: {url}")
    finally:
        _run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(wt, ignore_errors=True)
        _run(["git", "-C", str(REPO), "branch", "-D", branch])
