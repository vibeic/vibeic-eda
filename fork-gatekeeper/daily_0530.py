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
* It never force-pushes. A rejected push is reported, never overridden — on
  2026-07-31 a `master` that looked stale locally was 7924 upstream commits
  BEHIND its own origin, and forcing would have destroyed them.
* It never cuts an image version when no tool changed.
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
a dry run does. A case nobody decided is still `needs_human`; a case the
gatekeeper decided is decided.

The invocation BLOCKS. Detaching it would end this process before the
gatekeeper's turn produced anything, which is the exact failure that has already
cost this campaign four rounds on one cell.

Exit: 0 clean, 1 something needs a human, 2 nothing could be checked.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gk_state

# Overridable so the AI-handoff path can be exercised against a synthetic fork.
# The step it guards only fires on a real merge conflict, and a conflict that
# never happens on a given morning is not evidence the handoff works.
FORKS = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")
EDA = Path("/home/reyerchu/vibeic-eda")
OURS = ("reyer", "vibeic")


def sh(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd)


def out(*a, cwd=None):
    return sh(*a, cwd=cwd).stdout.strip()


def mainline(g):
    for c in ("main", "master"):
        if out(*g, "rev-parse", "--verify", "-q", f"refs/heads/{c}"):
            return c
    return None


def step1_upstream(g, main, rep):
    """Upstream -> master. FIRST, so branch merges land on the final base."""
    if not out(*g, "remote", "get-url", "upstream"):
        rep["upstream"] = "no upstream remote"
        return
    # The fetch's exit status decides whether the comparison below means anything.
    # Discarding it turns "we could not reach upstream" into "we are up to date":
    # rev-list then measures against whatever ref this clone last managed to fetch,
    # and a stale ref reports behind == 0. Measured 2026-08-04: one fork sat 12
    # commits behind for a full day while the round logged "already current" each
    # morning. Its clone had a failed gc 88 minutes before the round (gc.log:
    # "There are too many unreachable loose objects"), 7019 unreachable objects,
    # and the fetch in that state did not update the ref. Nothing noticed, because
    # nothing was looking.
    fr = sh(*g, "fetch", "upstream", "--quiet")
    if fr.returncode != 0:
        detail = (fr.stderr or fr.stdout or "").strip().splitlines()
        rep["upstream"] = ("FETCH FAILED (rc=%d) — upstream state is UNKNOWN, not current: %s"
                           % (fr.returncode, detail[-1][:160] if detail else "no error text"))
        return
    ub = (out(*g, "rev-parse", "--verify", "-q", "upstream/main")
          or out(*g, "rev-parse", "--verify", "-q", "upstream/master"))
    if not ub:
        rep["upstream"] = "no upstream branch"
        return
    behind = out(*g, "rev-list", "--count", f"{main}..{ub}")
    if behind == "0":
        rep["upstream"] = "already current"
        return
    r = sh(*g, "merge", "--no-edit", "-m",
           f"Merge upstream into {main} (daily 05:30)", ub)
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
        commits = [{"sha": s[:12],
                    "subject": out(*g, "log", "-1", "--format=%s", s),
                    "author": out(*g, "log", "-1", "--format=%an", s)}
                   for s in out(*g, "rev-list", f"{main}..{ub}").split()[:20]]
        sh(*g, "merge", "--abort")
        rep["upstream"] = f"CONFLICT taking {behind} upstream commit(s)"
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
    """
    sh(*g, "fetch", "origin", "--prune", "--quiet")
    # Exclude BOTH conventional mainline names, not just the one this clone
    # happens to have checked out. A dry run caught `asap7sc7p5t_28` offering
    # `origin/master` for deletion because the local mainline is `main` while the
    # fork's default branch is `master`; the comparison was against the local
    # name and the remote default did not match it.
    protected_names = {"main", "master", main.split("/")[-1]}
    out_b = []
    for b in out(*g, "for-each-ref", "--format=%(refname:short)",
                 "refs/remotes/origin").splitlines():
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


def _branches_an_image_pin_depends_on(g, eda_root=EDA):
    """Branches that contain a commit some image Dockerfile pins.

    Deleting one of these is how a pin becomes an orphan: the commit survives
    only as long as a branch reaches it, and afterwards the image builds from
    something git may garbage-collect. That is not hypothetical — `cocotb`'s pin
    `15f2d1017` is reachable from ZERO branches today for exactly this reason.

    A dry run offered `origin/vibeic/batch-honesty-integration` for deletion,
    which is the branch `NGSPICE_REF` points into. Pruning it would have
    manufactured a second cocotb.
    """
    pins = set()
    try:
        files = list((eda_root / "tools").glob("*/Dockerfile"))
        if (eda_root / "Dockerfile").is_file():
            files.append(eda_root / "Dockerfile")
        for f in files:
            for m in re.finditer(r"^ARG\s+\w*REF\w*=([0-9a-f]{7,40})",
                                 f.read_text(errors="replace"), re.M):
                pins.add(m.group(1))
    except Exception:                                       # noqa: BLE001
        return None          # cannot tell -> caller must not delete anything
    if not pins:
        return set()
    keep = set()
    for b in _fork_branches(g, mainline(g) or "master"):
        for p in pins:
            if sh(*g, "merge-base", "--is-ancestor", p, b).returncode == 0:
                keep.add(b)
                break
    return keep


def step2_ours(g, main, rep):
    """Our branches -> master, by patch equivalence."""
    brs = _fork_branches(g, main)
    merged, conflicted, empty = [], [], []
    for b in brs:
        cherry = out(*g, "cherry", main, b)
        new = [l.split()[1] for l in cherry.splitlines() if l.startswith("+")]
        if not new:
            empty.append(b)
            continue
        # only OUR commits are a reason to merge; an unmerged upstream commit
        # on a stale branch is not work, it is history.
        mine = [s for s in new if any(o in out(*g, "log", "-1", "--format=%an", s).lower()
                                      for o in OURS)]
        if not mine:
            empty.append(b)
            continue
        r = sh(*g, "merge", "--no-edit", "-m",
               f"consolidate: bring {b} onto {main} (one line, no long-lived branches)", b)
        if r.returncode:
            # Collect the evidence a merge-vs-cherry-pick decision needs BEFORE
            # aborting, because aborting destroys it. The owner's standing rule
            # is that this decision needs AI: "Merge 或者是 Cherry-pick 的話,
            # 是需要用到 AI 的, 用程式直接判斷是不夠的." A script that stops
            # here and prints needs_human has not made the decision, it has
            # only deferred it — and in practice nobody came back for it.
            files = [l for l in out(*g, "diff", "--name-only",
                                    "--diff-filter=U").splitlines() if l.strip()]
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


def _branches_serving_an_open_upstream_pr(g):
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

    So ask GitHub, not git. Failure to reach the API returns None, and the
    caller then prunes NOTHING: a branch kept by mistake costs a line of
    clutter, a branch deleted by mistake costs an upstream contribution.
    """
    origin = out(*g, "remote", "get-url", "origin")
    upstream = out(*g, "remote", "get-url", "upstream")
    if not origin or not upstream:
        return set()          # no upstream to have filed a PR against

    def _slug(url):
        u = url.strip().removesuffix(".git")
        if u.startswith("git@"):
            u = u.split(":", 1)[-1]
        parts = [p for p in u.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else ""

    up, org = _slug(upstream), _slug(origin).split("/")[0]
    if not up or not org:
        return set()

    r = sh("gh", "api", f"repos/{up}/pulls?state=open&per_page=100")
    if r.returncode:
        return None           # cannot tell -> caller must not delete anything
    try:
        prs = json.loads(r.stdout)
    except ValueError:
        return None
    return {p["head"]["ref"] for p in prs
            if ((p.get("head") or {}).get("repo") or {}).get("owner", {})
            .get("login", "").lower() == org.lower()}


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


def step4_prune(g, main, rep, apply):
    """Delete only what git itself says holds nothing unique -- AND what is not
    serving an open upstream PR -- AND what we created in the first place."""
    brs = [b for b in _fork_branches(g, main) if _is_ours_to_delete(g, b)]
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
    protected = _branches_serving_an_open_upstream_pr(g)
    if protected is None:
        rep["pruned"] = []
        rep["prune_skipped"] = ("could not reach the GitHub API to check for "
                                "open upstream PRs; pruned nothing rather than "
                                "risk closing one")
        rep["needs_human"] = True
        return
    if protected:
        rep["prune_protected"] = sorted(protected)
    gone = []
    for b in brs:
        cherry = out(*g, "cherry", main, b)
        if [l for l in cherry.splitlines() if l.startswith("+")]:
            continue                      # still holds unique work — keep
        if (b in protected
                or b.split("/", 1)[-1] in protected):
            continue                      # head of an open upstream PR — keep
        if apply:
            # `b` may be a remote-tracking name (origin/x) with no local ref.
            local = b.split("/", 1)[-1] if b.startswith("origin/") else b
            sh(*g, "branch", "-D", local)
            if sh(*g, "push", "-q", "origin", "--delete", local).returncode == 0:
                gone.append(b)
        else:
            gone.append(b)
    rep["pruned"] = gone


def _claude_bin():
    for c in (Path.home() / ".local/bin/claude",):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    r = subprocess.run(["bash", "-lic", "command -v claude"],
                       capture_output=True, text=True)
    p = r.stdout.strip()
    return p if p else None


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


def step2b_ai_decisions(report, state_dir, apply, timeout_s=3600):
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
            cases.append({**c, "fork": fork, "path": str(FORKS / fork)})
    if not cases:
        return {"cases": 0, "invoked": False}

    state_dir.mkdir(parents=True, exist_ok=True)
    pending = state_dir / "ai_decisions_pending.json"
    pending.write_text(json.dumps(cases, indent=2) + "\n")
    brief = _handoff_brief(cases)
    (state_dir / "ai_decisions_brief.txt").write_text(brief)

    if not apply:
        return {"cases": len(cases), "invoked": False,
                "note": "dry run — brief written, gatekeeper not invoked"}

    binp = _claude_bin()
    if not binp:
        return {"cases": len(cases), "invoked": False,
                "error": "no claude binary on this host — cases left pending"}

    log = state_dir / "ai_decisions.log"
    # BLOCKING on purpose. Detaching would end this process before the
    # gatekeeper's turn produced anything, which is the failure mode that has
    # already cost this campaign four rounds on one cell.
    r = subprocess.run([binp, "-p", brief,
                        "--permission-mode", "bypassPermissions"],
                       capture_output=True, text=True, timeout=timeout_s)
    log.write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""))
    return {"cases": len(cases), "invoked": True, "rc": r.returncode,
            "log": str(log),
            "tail": (r.stdout or "").strip()[-600:]}


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
    ap.add_argument("--ai-timeout", type=int, default=3600,
                    help="seconds to let the gatekeeper's turn run (default 3600)")
    args = ap.parse_args(argv)

    if not FORKS.is_dir():
        print("daily_0530: no forks root", file=sys.stderr)
        return 2

    report, needs_human = {}, False
    for d in sorted(FORKS.iterdir()):
        if not (d / ".git").is_dir():
            continue
        g = ["git", "-C", str(d)]
        main = mainline(g)
        if not main:
            report[d.name] = {"error": "no main/master"}
            continue
        if out(*g, "status", "--porcelain", "-uno", "--ignore-submodules=all"):
            report[d.name] = {"error": "dirty worktree — skipped, nothing discarded"}
            needs_human = True
            continue
        rep = {"main": main, "needs_human": False}
        if args.apply:
            sh(*g, "checkout", "-q", main)
            step1_upstream(g, main, rep)
            step2_ours(g, main, rep)
            # EQUAL TREES, DIFFERENT SHAS -> adopt the remote, do not push
            # (vibeic-eda#61). Checked BEFORE the push, because the push would
            # be rejected and that rejection is indistinguishable from a lost
            # race once it has happened.
            adopt = same_content_divergence(g, main)
            if adopt:
                # Re-prove losslessness immediately before acting: `reset --hard`
                # on a tree that is no longer equal would discard content.
                if out(*g, "rev-parse", f"{main}^{{tree}}") == \
                        out(*g, "rev-parse", f"origin/{main}^{{tree}}"):
                    _was = out(*g, "rev-parse", main)
                    sh(*g, "update-ref", f"refs/salvage/pre-adopt-{_was[:12]}", _was)
                    sh(*g, "reset", "--hard", f"origin/{main}")
                    rep["push"] = (f"ADOPTED origin/{main} {adopt[:12]} — same tree, "
                                   f"different commit; nothing of ours to publish")
                    rep["diverged_same_content"] = True
                    step4_prune(g, main, rep, True)
                else:
                    rep["push"] = ("SKIPPED — the trees stopped matching between "
                                   "the check and the reset")
                    rep["needs_human"] = True
            else:
                r = sh(*g, "push", "-q", "origin", main)
                if r.returncode == 0:
                    rep["push"] = "ok"
                    step4_prune(g, main, rep, True)
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
        needs_human |= rep.get("needs_human", False)
        report[d.name] = rep

    # Step 2b — the AI half. A conflict is NOT a reason to stop the round; it
    # is the one decision this script is not allowed to make alone.
    ai = step2b_ai_decisions(report, gk_state.state_dir(), args.apply and not args.no_ai,
                             timeout_s=args.ai_timeout)
    report["_ai_decisions"] = ai
    # Only an UNRESOLVED case needs a human. A case the gatekeeper decided is
    # decided; a case it could not reach still needs someone.
    if ai.get("cases") and not ai.get("invoked"):
        needs_human = True
    if ai.get("invoked") and ai.get("rc"):
        needs_human = True

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")

    for name, r in sorted(report.items()):
        if name == "_ai_decisions":
            continue
        if r.get("error"):
            print(f"  {name:<20} ⚠️ {r['error']}")
            continue
        bits = []
        if r.get("upstream"):
            bits.append(f"upstream: {r['upstream']}")
        if r.get("merged"):
            bits.append(f"merged {len(r['merged'])} branch(es)")
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
                  f"handed to the gatekeeper AI (rc={_ai.get('rc')}) "
                  f"— log: {_ai.get('log')}")
        else:
            print(f"\n  step 2b  {_ai['cases']} conflict(s) PENDING a gatekeeper "
                  f"decision: {_ai.get('error') or _ai.get('note')}")

    print("\n  step 5 (build + verify + publish) is daily_release.py — run it next"
          if not args.skip_build else "")
    return 1 if needs_human else 0


if __name__ == "__main__":
    sys.exit(main_())
