#!/usr/bin/env python3
"""Land the `gk-merge/<date>` PRs the round opened, so the round can converge.

OWNER OVERRIDE, 2026-08-22, recorded here because it reverses a stated design
decision rather than extending one. `prepare_merge_pr.py` opens these PRs "FOR
HUMAN REVIEW" and its header says, in as many words, `Never auto-merges` — the
safety argument being that a poisoned LLM verdict can at most PROPOSE a real
upstream commit, because "the human catches it on the PR".

That gate has caught something. vibeic/OpenROAD#6 was closed 2026-08-20 with:

    Superseded — the guard this carries is already on master, in the form
    upstream refactored it into.

which is a judgement that needed someone to read the code. This program would
have merged it.

The owner weighed that against the cost and chose to land them automatically:
an unlanded merge PR makes `fork_github_convergence` report open work, which
skips `build_page`, so the certified page silently stops publishing until a
human happens to look — and the only signal was `check_publish_silence`, days
later. `prepare_merge_pr.py` keeps its guarantee: it still never merges. The
override lives here, in one named place, and nowhere else.

What is still refused, because none of it was part of that decision:

  * a PR whose head is not `gk-merge/…`      — not one of ours to land
  * a repository outside the fork registry   — not ours at all
  * DRAFT, or not OPEN                       — not offered for landing
  * not MERGEABLE, or mergeStateStatus != CLEAN — git says no; taking it further
                                               would mean resolving a conflict
                                               no one reviewed

    rc 0  nothing to land, or everything landed
    rc 1  something was offered and could not be landed (named, per PR)
    rc 2  NORECORD — the registry or GitHub could not be read
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

RC_OK, RC_REFUSED, RC_NORECORD = 0, 1, 2

MERGE_BRANCH_PREFIX = "gk-merge/"


def _sh(*argv, timeout: int = 120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{argv[0]} failed: {exc}"


def _registry(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable: {exc}"
    org = doc.get("org")
    forks = doc.get("forks")
    if not isinstance(org, str) or not isinstance(forks, list) or not forks:
        return None, f"{path} does not carry an org and a fork list"
    tools = [f.get("tool") for f in forks if isinstance(f, dict) and f.get("tool")]
    if not tools:
        return None, f"{path} lists no named forks"
    return [f"{org}/{t}" for t in tools], ""


#: How long to wait for GitHub to compute a merge state, and how often to ask.
#:
#: A PR opened seconds ago has NO computed mergeability — GitHub works it out on
#: first request and recomputes it whenever the base moves. Landing in the round
#: that opened the PR means asking at exactly the moment the answer is missing,
#: so a single query would refuse every time and the whole point would be lost.
#: Bounded, because "not computed yet" must eventually become a refusal rather
#: than an indefinite wait inside a round.
MERGE_STATE_ATTEMPTS = 6
MERGE_STATE_WAIT_S = 5.0


def _settled(repo: str, pr: dict, sleep=time.sleep) -> dict:
    """The PR with a computed merge state, or the last unknown answer."""
    for attempt in range(MERGE_STATE_ATTEMPTS):
        if pr.get("mergeable") != "UNKNOWN":
            return pr
        sleep(MERGE_STATE_WAIT_S)
        rc, out = _sh("gh", "pr", "view", str(pr.get("number")), "-R", repo,
                      "--json", "number,headRefName,isDraft,mergeable,"
                                "mergeStateStatus,title")
        if rc != 0:
            return pr
        try:
            pr = json.loads(out)
        except ValueError:
            return pr
    return pr


def _open_merge_prs(repo: str):
    rc, out = _sh("gh", "pr", "list", "-R", repo, "--state", "open",
                  "--json", "number,headRefName,isDraft,mergeable,"
                            "mergeStateStatus,title")
    if rc != 0:
        return None, out.strip()[:200]
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return None, "gh returned unparseable JSON"
    return [r for r in rows
            if str(r.get("headRefName") or "").startswith(MERGE_BRANCH_PREFIX)], ""


def _land(repo: str, pr: dict, dry: bool):
    n = pr.get("number")
    if pr.get("isDraft"):
        return False, f"{repo}#{n} is a DRAFT — not offered for landing"
    # UNKNOWN IS NOT A REFUSAL BY GIT. GitHub computes mergeability lazily and
    # recomputes it whenever the base moves — which a round does to its own
    # forks — so the first query after a merge returns UNKNOWN while the answer
    # is still being worked out. Reporting that as "git says no" was untrue and
    # would have made every freshly-moved fork look conflicted.
    if pr.get("mergeable") == "UNKNOWN":
        return False, (f"{repo}#{n} has no computed merge state yet (GitHub "
                       f"recomputes it when the base moves). Not landed this "
                       f"round; the next one asks again")
    if pr.get("mergeable") != "MERGEABLE" or pr.get("mergeStateStatus") != "CLEAN":
        return False, (f"{repo}#{n} is {pr.get('mergeable')}/"
                       f"{pr.get('mergeStateStatus')} — git says no, and taking it "
                       f"further would mean resolving a conflict no one reviewed")
    if dry:
        return True, f"DRY-RUN — would merge {repo}#{n} ({pr.get('title','')[:60]})"
    # A merge commit, not a squash: the whole safety argument for these PRs is
    # that the diff IS the real upstream commits, and squashing erases which
    # ones they were.
    rc, out = _sh("gh", "pr", "merge", str(n), "-R", repo, "--merge",
                  "--delete-branch", timeout=180)
    if rc != 0:
        return False, f"{repo}#{n} merge failed: {out.strip()[:200]}"
    return True, f"merged {repo}#{n} ({pr.get('title','')[:60]})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--forks-json", default=str(_HERE / "FORKS.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    dry = a.dry_run or os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes")

    repos, why = _registry(Path(a.forks_json))
    if repos is None:
        print(f"[NORECORD] {why}")
        return RC_NORECORD

    landed, refused, unreadable = [], [], []
    for repo in repos:
        prs, err = _open_merge_prs(repo)
        if prs is None:
            unreadable.append(f"{repo}: {err}")
            continue
        for pr in prs:
            ok, note = _land(repo, _settled(repo, pr), dry)
            (landed if ok else refused).append(note)

    for note in landed:
        print(f"  [LANDED] {note}")
    for note in refused:
        print(f"  [REFUSED] {note}")
    for note in unreadable:
        print(f"  [COULD NOT ASK] {note}")

    if unreadable and not landed and not refused:
        print(f"[NORECORD] no fork could be asked ({len(unreadable)} error(s))")
        return RC_NORECORD
    if refused:
        print(f"[REFUSED] {len(refused)} merge PR(s) were offered and not landed")
        return RC_REFUSED
    print(f"[PASS] {len(landed)} merge PR(s) landed" if landed
          else "[PASS] no merge PR is waiting")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
