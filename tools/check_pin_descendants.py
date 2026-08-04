#!/usr/bin/env python3
"""Does each moved pin still CONTAIN what the old pin contained?

WHY THIS EXISTS (vibeic-eda#86)
===============================
`check_pins_agree.py` asserts a pin is stated identically in all three places.
`check_pins_current.py` asserts a pin is the TIP of its fork branch. Neither can
see the defect below, and neither could have.

Measured, 2026-07-19. `393a75f` ("image 0.2.22: 12-fork consolidation") moved

    ARG NGSPICE_REF=cdb4fae2db97...  ->  6e9f78fb5dd5...

Both are real commits on real vibeic branches. All three pin sites agreed. The
new pin was the tip of its branch. Every existing gate was green. And the move
silently dropped TWO commits that were never re-applied anywhere:

    0b63933a1  Latin-Hypercube sampling for native Monte-Carlo
    cdb4fae2d  .mc dot-card + DC gshunt-homotopy last-resort

so three capabilities left the shipped image. `FIX_STATUS.md` went on asserting
all three for 17 days, correctly as of when it was written and wrongly from that
commit onward, because nothing re-checked the claims when the pin moved.

`check_no_capability_lost.py` cannot catch this either, and the reason is
structural rather than a bug: it compares our image against the BASE image's
commands. Losing one of OUR OWN patches is invisible to it — the base never had
that patch, so its absence is not a difference.

WHAT THIS CHECKS
================
For every `ARG <TOOL>_REF` that changed between BASE and HEAD, ask whether the
new pin is a DESCENDANT of the old one. If it is, nothing the old pin contained
can have been lost, because every commit reachable from the old is reachable
from the new. That is a predicate, not a heuristic.

    status ahead / identical  ->  descendant. Nothing dropped.
    status behind / diverged  ->  SIDEWAYS. Commits present at the old pin are
                                  absent at the new one, and this names them.

WHY A SIDEWAYS MOVE IS NOT AUTOMATICALLY WRONG
==============================================
Sometimes dropping is right. The gshunt rung in `cdb4fae2d` above was superseded
eleven days later by a better mechanism and SHOULD have gone. A consolidation
onto a mainline, or a rebase onto a new upstream line, will also trip this. So
the guard has an escape hatch — and the escape hatch is the part that decides
whether this program is worth anything.

Measured over the repo's whole history (87 sha->sha pin transitions): 73 are
descendants and would stay green; 10 are sideways. Ten in two months, not
dozens, so the hatch is used rarely enough to stay meaningful. Of those ten the
largest dropped 17 commits, which is why the declaration asks for a COUNT rather
than a list of shas: a 17-sha declaration is the kind of ceremony people delete
the check to avoid, and the noticing does not happen in the typing.

It happens in the READING. When this fires it prints every dropped commit with
its subject, then requires the commit that makes the move to carry

    PIN-DROPS: <repo>=<count>

with the count matching what was actually dropped. You cannot write that line
without having run the check and looked at the list, you cannot copy it from a
previous move (the repo and count differ), and a wrong count is still RED. For
`393a75f` the required line would have been `PIN-DROPS: ngspice=2` — written
directly beneath a two-line list naming `.mc`, the LHS sampler and the gshunt
rung. That is the sentence nobody had to write.

EXIT CODES follow the convention of the other checks in this directory:
    0  every moved pin is a descendant, or its drop is correctly declared
    1  a finding: a pin moved sideways and was not declared (or declared wrong)
    2  could not determine. NOT a pass. A guard that reports "fine" when it
       could not look is the exact failure this repo has now hit three times.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

#: `ARG <NAME>_REF=<value>` on the added/removed side of a diff.
DIFF_ARG_RE = re.compile(r"^([+-])ARG\s+([A-Z0-9_]+_REF)=(\S+)")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
#: `github.com/vibeic/<repo>` — the same pairing rule `check_pins_current`
#: uses, so a tool named in two places resolves the same way in both.
REPO_RE = re.compile(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]")
#: The escape hatch. `<repo>` as GitHub spells it; `<count>` must match.
DECLARE_RE = re.compile(r"^\s*PIN-DROPS:\s*([A-Za-z0-9_.-]+)\s*=\s*(\d+)\s*$",
                        re.M | re.I)

PIN_FILE_RE = re.compile(r"^(Dockerfile|tools/[^/]+/Dockerfile)$")


class Undetermined(Exception):
    """Raised when the answer is unknown. Never rendered as a pass."""


def _sh(cmd: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                    # noqa: BLE001
        return 1, "", str(exc)


def _git(root: Path, *a: str) -> Tuple[int, str, str]:
    return _sh(["git", "-C", str(root), *a])


def pin_files(root: Path, base: str, head: str) -> List[str]:
    rc, out, _ = _git(root, "diff", "--name-only", f"{base}...{head}")
    if rc != 0:
        raise Undetermined(f"cannot diff {base}...{head}")
    return [f for f in out.split() if PIN_FILE_RE.match(f)]


def transitions(root: Path, base: str, head: str
                ) -> List[Tuple[str, str, str, str]]:
    """(file, ARG, old, new) for every pin whose VALUE changed."""
    out: List[Tuple[str, str, str, str]] = []
    for f in pin_files(root, base, head):
        rc, diff, _ = _git(root, "diff", "-U0", f"{base}...{head}", "--", f)
        if rc != 0:
            raise Undetermined(f"cannot diff {f}")
        removed: Dict[str, str] = {}
        added: Dict[str, str] = {}
        for ln in diff.splitlines():
            m = DIFF_ARG_RE.match(ln)
            if not m:
                continue
            sign, name, val = m.groups()
            (removed if sign == "-" else added)[name] = val.split("#")[0].strip()
        for name in sorted(set(removed) & set(added)):
            if removed[name] != added[name]:
                out.append((f, name, removed[name], added[name]))
    return out


def _repo_holds(repo: str, *shas: str) -> bool:
    """Does `vibeic/<repo>` contain every one of these commits?"""
    for s in shas:
        rc, _, _ = _sh(["gh", "api", f"repos/vibeic/{repo}/commits/{s}",
                        "--jq", ".sha"])
        if rc != 0:
            return False
    return True


def repo_for(root: Path, rev: str, path: str, arg: str,
             shas: Tuple[str, ...] = ()) -> Optional[str]:
    """Which vibeic repo does `arg` feed, in `path` as of `rev`?

    Name-pairing against the `github.com/vibeic/<repo>` URLs in the same file,
    not position: `tools/lvs/Dockerfile` builds magic AND netgen from one file,
    so a first-match parse silently drops one of them.

    FALLBACK, and why it is not a hole. `OPEN_PDKS_REF` names a real mirror of
    ours whose URL never appears in the composing Dockerfile — the PDK is
    consumed as a prebuilt volume, so nothing clones it there. URL-pairing alone
    therefore returned "unknown" and this program answered rc 2 on four historical
    commits. Guessing `vibeic/<stem>` on its own WOULD be a hole, so the guess is
    only accepted when that repo actually CONTAINS both commits being compared —
    a repo holding both the old and new pin is not a coincidence.
    """
    rc, text, _ = _git(root, "show", f"{rev}:{path}")
    if rc != 0:
        return None
    stem = arg[:-4] if arg.endswith("_REF") else arg
    flat = stem.replace("_", "").upper()
    repos = list(dict.fromkeys(REPO_RE.findall(text)))
    for repo in repos:
        if re.sub(r"[-._]", "", repo).upper() == flat:
            return repo
    if len(repos) == 1:
        return repos[0]
    if shas:
        guess = stem.lower()
        if _repo_holds(guess, *shas):
            return guess
    return None


def compare(repo: str, old: str, new: str) -> dict:
    """GitHub's view of old -> new. Raises Undetermined rather than guessing."""
    rc, out, err = _sh(["gh", "api", f"repos/vibeic/{repo}/compare/{old}...{new}",
                        "--jq", "{status:.status,behind_by:.behind_by,"
                                "ahead_by:.ahead_by}"])
    if rc != 0:
        raise Undetermined(f"{repo}: compare {old[:9]}...{new[:9]} failed: "
                           f"{(err or out).strip()[:160]}")
    try:
        return json.loads(out)
    except ValueError:
        raise Undetermined(f"{repo}: compare returned unparseable JSON")


def dropped_commits(repo: str, old: str, new: str) -> List[Tuple[str, str]]:
    """The commits reachable from the OLD pin and not from the new one.

    This is the reverse compare: `new...old` lists what old has that new lacks.
    """
    rc, out, err = _sh(["gh", "api", f"repos/vibeic/{repo}/compare/{new}...{old}",
                        "--jq", '.commits[] | "\\(.sha) \\(.commit.message '
                                '| split("\\n")[0])"'])
    if rc != 0:
        raise Undetermined(f"{repo}: reverse compare failed: "
                           f"{(err or out).strip()[:160]}")
    rows = []
    for ln in out.splitlines():
        sha, _, subj = ln.partition(" ")
        if sha:
            rows.append((sha, subj))
    return rows


def declarations(root: Path, base: str, head: str) -> Dict[str, int]:
    """`PIN-DROPS: <repo>=<n>` collected from the commit messages BASE..HEAD."""
    rc, out, _ = _git(root, "log", "--format=%B%x00", f"{base}..{head}")
    if rc != 0:
        raise Undetermined(f"cannot read commit messages for {base}..{head}")
    found: Dict[str, int] = {}
    for m in DECLARE_RE.finditer(out):
        found[m.group(1).lower()] = int(m.group(2))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--offline", action="store_true",
                    help="skip the network half and SAY SO. Not a clean result.")
    a = ap.parse_args(argv)

    try:
        moves = transitions(a.root, a.base, a.head)
    except Undetermined as exc:
        print(f"check_pin_descendants: could not determine — {exc}",
              file=sys.stderr)
        return 2

    touched = pin_files(a.root, a.base, a.head)
    if not moves:
        # A pin file can change without a pin moving (a comment, a build stage).
        # Say which of the two happened rather than printing a bare success that
        # reads the same as "every pin is fine".
        if touched:
            print(f"check_pin_descendants: {len(touched)} pin file(s) changed in "
                  f"{a.base}..{a.head} but no ARG *_REF VALUE moved — nothing to "
                  f"compare")
        else:
            print(f"check_pin_descendants: no pin file changed in "
                  f"{a.base}..{a.head} — nothing to compare")
        return 0

    if a.offline:
        print(f"check_pin_descendants: SKIPPED {len(moves)} pin move(s) "
              f"(--offline). This is NOT a clean result — the descendant "
              f"question is answered over the network or not at all.")
        return 0

    try:
        declared = declarations(a.root, a.base, a.head)
    except Undetermined as exc:
        print(f"check_pin_descendants: could not determine — {exc}",
              file=sys.stderr)
        return 2

    findings: List[str] = []
    ok = 0
    for path, arg, old, new in moves:
        if not (SHA_RE.match(old) and SHA_RE.match(new)):
            # A branch-name pin is not a commit and has no ancestry to compare.
            # `check_pins_agree` already rejects those; not this program's job.
            print(f"  n/a   {arg}: {old} -> {new} (not a commit-to-commit move)")
            continue
        repo = repo_for(a.root, a.head, path, arg, (old, new))
        if repo is None:
            print(f"check_pin_descendants: could not determine which vibeic "
                  f"repo {arg} feeds in {path}", file=sys.stderr)
            return 2
        try:
            cmp_ = compare(repo, old, new)
            status = cmp_.get("status")
            if status in ("ahead", "identical"):
                print(f"  PASS  {repo:22s} {old[:9]} -> {new[:9]}  "
                      f"descendant (+{cmp_.get('ahead_by', 0)})")
                ok += 1
                continue
            drops = dropped_commits(repo, old, new)
        except Undetermined as exc:
            print(f"check_pin_descendants: could not determine — {exc}",
                  file=sys.stderr)
            return 2

        want = len(drops)
        got = declared.get(repo.lower())
        head_line = (f"  {repo:22s} {old[:9]} -> {new[:9]}  SIDEWAYS ({status}) "
                     f"— {want} commit(s) at the old pin are absent at the new one:")
        listing = "\n".join(f"      {s[:9]}  {subj}" for s, subj in drops)

        if got == want:
            print(f"  DECL  {repo:22s} {old[:9]} -> {new[:9]}  sideways, "
                  f"{want} dropped, DECLARED")
            print(listing)
            ok += 1
            continue

        print(f"  FAIL{head_line}")
        print(listing)
        if got is None:
            findings.append(
                f"{repo}: {want} commit(s) dropped and no declaration. If this "
                f"is deliberate, add `PIN-DROPS: {repo}={want}` to the commit "
                f"message that moves the pin.")
        else:
            findings.append(
                f"{repo}: declared `PIN-DROPS: {repo}={got}` but {want} "
                f"commit(s) are actually dropped.")

    if findings:
        print(f"\ncheck_pin_descendants: {len(findings)} pin move(s) drop "
              f"commits without a matching declaration\n", file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        print("\n  A pin that moves sideways does not fail the build. It ships "
              "an image\n  missing something the previous image had, and every "
              "claim written\n  against the old image stays on the page saying "
              "otherwise.", file=sys.stderr)
        return 1

    print(f"check_pin_descendants: {ok} moved pin(s) checked, all either "
          f"descendants or declared")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Undetermined as exc:
        # An uncaught exception that escapes main() exits 1, and rc 1 here means
        # "a pin moved sideways" — a crash would be read as a finding about the
        # tree. It is not: it is a finding about this program.
        print(f"check_pin_descendants: could not determine — {exc}",
              file=sys.stderr)
        sys.exit(2)
    except Exception as exc:                                    # noqa: BLE001
        print(f"check_pin_descendants: crashed — {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(2)
