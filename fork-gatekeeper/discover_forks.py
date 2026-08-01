#!/usr/bin/env python3
"""discover_forks.py — build the per-tool FORK ledger from REAL state.

Source of truth for what we currently ship = the **vibeic-eda Dockerfile**: it pins
each fork with `ARG <TOOL>_REF=<sha>` (a commit on our `vibeic/*` enhancement branch)
and clones+checks-out that ref. So "our current version" is the pinned REF, and our
carried patches are the commits on that ref since its merge-base with upstream.

An ARG of its own is not the only way in. A fork can be VENDORED inside another fork —
declared as a git submodule of a pinned repository — and reach the image when the stage
that clones the host runs `git submodule update --init`. `integrated` therefore means
"reaches the shipped image", not "has an ARG": both routes are discovered, and the
indirect one records HOW (see `expand_vendored_pins`). vibeic/vibeic-eda#8: OpenSTA
ships as OpenROAD's `src/sta`, and was reported as "not in the image, nothing to sync"
for as long as the detector's model was one ARG per tool.

Limit of the derivation, stated because it bounds the claim: this reads the Dockerfile's
BUILD GRAPH (what is pinned and fetched), not the final image's filesystem. A build
stage whose artifacts are never `COPY --from`ed would still count as integrated. That is
the pre-existing assumption for ARG pins; vendoring inherits it rather than widening it.

Tracking granularity is **releases, not commits** (owner directive): for each tool we
compare the release our pin is based on against the upstream's newer releases. A new
upstream release is the merge candidate; the daily gatekeeper rebases our branch onto
it, bumps the Dockerfile ARG, and rebuilds the vibeic-eda image (the green gate).

    python3 discover_forks.py            # refresh ledger/<tool>.json + ledger/index.json

EXIT STATUS — three outcomes, not two. `0` the sweep measured everything and
nothing contradicts itself; `1` at least one row made a claim its own repository
REFUTES (a defect in us, printed under "BUCKET INVARIANT VIOLATED"); `2` the sweep
ran and at least one release could not be decided at all, so that tool's
`behind_releases` and `base_release` are withheld. 2 is not a refutation and must
not be read as one — it is a slow disk, a clone missing an object, an exhausted
API budget. It is also not success, which is what it used to report.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).parent          # version-controlled source
sys.path.insert(0, str(HERE))
import gk_state  # noqa: E402 — WHERE state lives and WHO may write it (vibeic/vibeic-eda#12)

STATE = gk_state.state_dir()
LEDGER = STATE / "ledger"             # runtime state — outside the source tree
FORKS = json.loads((HERE / "FORKS.json").read_text())["forks"]
ORG = "vibeic"
EDA_REPO = "vibeic/vibeic-eda"
CAP = 200


def gh(path: str):
    r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"_err": r.stderr.strip().splitlines()[-1][:160] if r.stderr else "error"}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"_err": "parse error"}


def _gh_file(repo: str, path: str, ref: str | None = None) -> str | None:
    d = gh(f"repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else ""))
    if isinstance(d, dict) and d.get("content"):
        try:
            return base64.b64decode(d["content"]).decode("utf-8", "replace")
        except Exception:
            return None
    return None


def _instructions(text: str) -> list[tuple[int, int, str]]:
    """The Dockerfile's instructions with `\\`-continuations joined: [(start, end, text)].

    The instruction is the unit that decides whether a vendored subtree is fetched: the
    `git clone …vibeic/X.git` and the `git submodule update --init` that pulls X's
    submodules are steps of ONE `RUN`, and only that RUN's own text says whether the
    second step is there. Offsets are kept so a match found in the raw text can be mapped
    back to the instruction that contains it.
    """
    out: list[tuple[int, int, str]] = []
    start, buf, pos = None, [], 0
    for line in text.splitlines(keepends=True):
        if start is None:
            start = pos
        buf.append(line)
        pos += len(line)
        if not line.rstrip("\n").rstrip().endswith("\\"):
            out.append((start, pos, "".join(buf)))
            start, buf = None, []
    if buf:
        out.append((start or 0, pos, "".join(buf)))
    return out


# `git submodule update … --init` inside ONE shell step: the `[^&|;\n]` class stops the
# search at the next command so an unrelated `--init` further down the RUN cannot be read
# as this clone's, while the escaped-newline alternative lets the flags be wrapped.
_SUBMODULE_INIT = re.compile(r"git\s+submodule\s+update(?:[^&|;\n]|\\\s*\n)*?\s--init\b")
_SUBMODULE_RECURSIVE = re.compile(
    r"git\s+submodule\s+update(?:[^&|;\n]|\\\s*\n)*?\s--recursive\b")


def parse_dockerfile_pins(text: str) -> dict:
    """tool(lowercased repo name) -> {'ref': sha, 'arg': 'YOSYS_REF', 'branch': 'vibeic/…',
    'repo': 'yosys', 'submodules': bool, 'recursive': bool}.

    `submodules`/`recursive` describe the clone step, not the repository: they say whether
    the build FETCHES this fork's submodules (and their submodules), which is what decides
    whether a fork vendored inside it reaches the image. `expand_vendored_pins` consumes
    them; a declared-but-never-fetched submodule is correctly not integrated.
    """
    orig_text = text          # before ${*_REPO} substitution, for the clone-step lookup
    args = dict(re.findall(r"ARG\s+(\w+_REF)\s*=\s*(\S+)", text))
    # Since the per-tool split (vibeic-eda#14) a clone reads `git clone "${YOSYS_REPO}"`
    # and the URL lives in the ARG default. Substituting the *_REPO args in before
    # matching keeps the URL where this parser has always looked for it: without this
    # the regex below finds nothing and every tool silently drops to default-branch
    # tracking, which reads exactly like a fork with no pin.
    for _rv, _url in re.findall(r"ARG\s+(\w+_REPO)\s*=\s*(\S+)", text):
        text = text.replace("${%s}" % _rv, _url)
    branches = {}
    for m in re.finditer(r"ARG\s+(\w+_REF)\s*=\s*\S+\s*#[^\n]*branch\s+(\S+)", text):
        branches[m.group(1)] = m.group(2)
    instrs = _instructions(text)
    pins = {}
    for m in re.finditer(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)\.git", text):
        tool = m.group(1)
        # Dispatched on the clone's SYNTAX, not on distance. Four attempts at
        # "nearest ref" each fixed one pairing and broke another, because the two
        # forms put the ref on opposite sides of the URL:
        #
        #   [A] git clone --depth 1 --branch ${ORFS_REF} … <url>     ref BEFORE
        #   [B] git clone "${MAGIC_REPO}" /magic && … checkout ${MAGIC_REF}   AFTER
        #
        # Looking only forward (the original) missed every [A]: OpenROAD-flow-scripts
        # and ASAP7_for_KLayout parsed as having NO pin while both supply PDK trees
        # to the shipped image (vibeic-eda#32). Looking backward, or at the whole
        # instruction, mispaired the multi-clone RUNs — three asap7 clones in one
        # RUN, and lvs/sat-solvers whose provenance printf names both tools' refs.
        # A wrong pin is worse than a missing one: the row reads as tracked.
        #
        # `--branch X` and `checkout X` are unambiguous, so each form is matched
        # where it actually writes the ref.
        _tail = text[m.end(): m.end() + 400]
        _head = text[max(0, m.start() - 200): m.start()]
        # On the [A] side, LAST match wins: three clones sharing a RUN put several
        # `--branch` refs in the look-behind, and the one belonging to this URL is
        # the closest, not the first. `re.search` returns the first, which paired
        # asap7_pdk_r1p7 with asap7sc7p5t_28's ref.
        _b = re.findall(r"--branch\s+\$\{(\w+_REF)\}", _head)
        am = re.search(r"checkout\s+\$\{(\w+_REF)\}", _tail)
        if not am and _b:
            class _M:
                def __init__(self, g): self._g = g
                def group(self, _n): return self._g
            am = _M(_b[-1])
        if not am:
            am = re.search(r"\$\{(\w+_REF)\}", _tail)
        if am and am.group(1) in args:
            arg = am.group(1)
            # The instruction that CLONES this repo, not merely one the URL
            # text appears in. After `${X_REPO}` substitution the URL occurs
            # SEVERAL times — in the ARG that defines it, in the RUN that
            # clones, and in the provenance `printf` that records it — and this
            # loop assigns on every match, so the LAST one wins. For
            # `tools/openroad/Dockerfile` that last match is the printf:
            #
            #   1252  ARG OPENROAD_REPO=…            clone=no   submodule=no
            #   2885  RUN git clone … && git submodule update --init --recursive
            #   5279  RUN printf '{"tool":"openroad","repo":"%s"…'   <- wins
            #
            # so `submodules`/`recursive` came out False for a build that fetches
            # them, and `expand_vendored_pins` never fired for the one fork it
            # was written for: OpenSTA, vendored at src/sta, shipping in the
            # image while the ledger called it absent (vibeic-eda#8, #32).
            step = next((t for s, e, t in instrs if s <= m.start() < e), "")
            if "git clone" not in step:
                _needles = [m.group(0)]
                for _rv, _url in re.findall(r"ARG\s+(\w+_REPO)\s*=\s*(\S+)", orig_text):
                    if m.group(0) in _url:
                        _needles.append("${%s}" % _rv)
                step = next((t for _s, _e, t in instrs
                             if "git clone" in t and any(n in t for n in _needles)),
                            step)
            pins[tool.lower()] = {"ref": args[arg], "arg": arg, "branch": branches.get(arg),
                                  "repo": tool,
                                  "submodules": bool(_SUBMODULE_INIT.search(step)),
                                  "recursive": bool(_SUBMODULE_RECURSIVE.search(step))}
    return pins


def parse_gitmodules(text: str) -> list[dict]:
    """[{'path', 'url'}] — one entry per submodule declared in a `.gitmodules`.

    A section with no explicit `path` takes its NAME as the path, which is what git does
    and what the real OpenROAD `.gitmodules` relies on (`[submodule "src/sta"]` with a
    url and no path). Later declarations of one path win, as they do for git.
    """
    by_path: dict[str, dict] = {}
    name = path = url = None

    def _flush():
        p = path or name
        if p and url:
            by_path[p] = {"path": p, "url": url}

    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r'\[submodule\s+"?([^"\]]+)"?\]', line)
        if m:
            _flush()
            name, path, url = m.group(1), None, None
            continue
        m = re.match(r"(path|url)\s*=\s*(\S+)", line)
        if m:
            if m.group(1) == "path":
                path = m.group(2)
            else:
                url = m.group(2)
    _flush()
    return list(by_path.values())


def submodule_repo(url: str, host_repo: str) -> str | None:
    """'owner/name' for a GitHub submodule URL, or None if it names no GitHub repo.

    Git's RELATIVE form is resolved against the HOST repository, not treated as a literal
    path — that is not a corner case: the live OpenROAD `.gitmodules` pins abc as
    `../../The-OpenROAD-Project/abc.git`, which reaches a different owner from
    `vibeic/OpenROAD`, and a reader that ignored the `..` segments would attribute it to
    us and try to track a repository we do not own.
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith(("./", "../")):
        segs = host_repo.split("/")
        rest = url.split("/")
        while rest and rest[0] in (".", ".."):
            if rest[0] == ".." and segs:
                segs.pop()
            rest.pop(0)
        segs += rest
    else:
        m = re.match(r"(?:https?://|git://|ssh://(?:[^@]+@)?)?(?:[^@/]+@)?github\.com[:/](.+)$",
                     url)
        if not m:
            return None
        segs = m.group(1).split("/")
    if len(segs) != 2 or not all(segs):
        return None
    return f"{segs[0]}/{segs[1][:-4] if segs[1].endswith('.git') else segs[1]}"


def _gitlink_sha(repo: str, ref: str, path: str) -> str | None:
    """The commit a host repository's tree records at a submodule path — the exact
    version of the vendored fork the image builds. This is the MECHANISM; the Dockerfile
    comment that documents the same relationship in prose is not read."""
    d = gh(f"repos/{repo}/contents/{path}?ref={ref}")
    if isinstance(d, dict) and d.get("type") == "submodule" and d.get("sha"):
        return d["sha"]
    return None


def _branch_at_head(repo: str, sha: str) -> str | None:
    """The branch of `repo` whose HEAD is `sha`, when exactly one is — else None.

    A gitlink is a bare commit; the merge-PR path needs a branch name to propose onto.
    Ambiguity (no branch, or several) resolves to None, which `prepare_merge_pr` already
    handles by declining to open a PR rather than guessing which branch was meant.
    """
    d = gh(f"repos/{repo}/commits/{sha}/branches-where-head")
    if isinstance(d, list) and len(d) == 1 and isinstance(d[0], dict):
        return d[0].get("name")
    return None


def expand_vendored_pins(pins: dict, max_depth: int = 3) -> dict:
    """Add the forks that reach the image INSIDE another fork's pinned ref.

    vibeic/vibeic-eda#8. A fork with no `ARG <TOOL>_REF` of its own is still shipped when
    a pinned fork declares it as a submodule AND that fork's clone step initialises
    submodules: the content is fetched and built with the host, so it is in the image as
    surely as an ARG-pinned clone. The detector modelled one ARG per tool, so such a fork
    read as `integrated=False` → "uses upstream directly — nothing to sync", about a tool
    whose enhancements the image actually runs.

    Both halves are STRUCTURAL and neither is the Dockerfile's prose: `.gitmodules` at the
    host's pinned ref names the repository, the tree's gitlink at that path names the
    commit, and the RUN that clones the host says whether they are fetched at all. Only
    submodules that resolve to OUR org are followed — a fork's third-party submodules
    (yosys carries seven) are somebody else's code, not a fork of ours to track.

    Recursion follows `--recursive`, bounded by `max_depth`, and never re-enters a tool
    that already has a pin: a direct ARG is the more specific statement of how a tool
    ships and is never overwritten by a vendored one.
    """
    out = dict(pins)
    frontier = [p for p in pins.values() if p.get("submodules") and p.get("ref")]
    for _ in range(max_depth):
        nxt = []
        for host in frontier:
            host_repo = f"{ORG}/{host.get('repo') or ''}"
            gm = _gh_file(host_repo, ".gitmodules", host["ref"])
            if not gm:
                continue
            for sub in parse_gitmodules(gm):
                repo = submodule_repo(sub["url"], host_repo)
                if not repo or repo.split("/")[0].lower() != ORG.lower():
                    continue
                name = repo.split("/")[1]
                if name.lower() in out:
                    continue
                sha = _gitlink_sha(host_repo, host["ref"], sub["path"])
                if not sha:
                    continue
                pin = {"ref": sha, "arg": host.get("arg"),
                       "branch": _branch_at_head(repo, sha), "repo": name,
                       # the host's submodules are fetched recursively or not at all, so
                       # this fork's own submodules ship exactly when --recursive was used
                       "submodules": bool(host.get("recursive")),
                       "recursive": bool(host.get("recursive")),
                       "vendored_in": host.get("repo"), "vendored_path": sub["path"],
                       "host_ref": host["ref"]}
                out[name.lower()] = pin
                nxt.append(pin)
        frontier = [p for p in nxt if p.get("submodules")]
        if not frontier:
            break
    return out


def _commit_brief(c: dict) -> dict:
    commit = c.get("commit", {})
    return {"sha": (c.get("sha") or "")[:12],
            "title": (commit.get("message") or "").splitlines()[0][:120] if commit else "",
            "date": ((commit.get("author") or {}).get("date", "") if commit else "")[:10],
            "url": c.get("html_url", "")}


#: A sort key that is not a date must not outrank one that is — see _iso_date.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_date(raw) -> str:
    """`YYYY-MM-DD` from an ISO timestamp, or "" if it is not one.

    These dates are SORT KEYS, compared as strings. A value that is not a date
    but is not empty either sorts by its first character: `"T00:00:00"` is
    greater than `"2026-06-30"` because `T` > `2`, so a malformed entry would
    take the latest slot and become the ref the ancestry compare runs against.
    Anything that does not start with a real date becomes "", which sorts last.
    """
    if not isinstance(raw, str):
        return ""
    head = raw[:10]
    return head if _ISO_DATE_RE.fullmatch(head) else ""


def _tags_by_date(up_full: str, limit: int = 30) -> list[dict] | None:
    """Tags newest-first WITH dates, in one call.

    `None` = COULD NOT ASK. `[]` = asked, and this repository has no tags.

    vibeic-eda#49. All three failure paths used to return `[]`, which is also a
    legitimate and common answer, and the docstring said so — "[] if it could not
    be asked" — naming the conflation without closing it. `_releases` then merged
    that list, so a repository whose tag feed could not be read presented as one
    with nothing to be behind, in the `measured` status. That is the sentence #47
    removed from the containment path, one function upstream of it: containment
    can now say "I could not decide", but it was being handed an input that had
    already decided, wrongly and silently.

    The REST tags endpoint gives neither a date nor a meaningful order — it
    returned `v2.0` first for OpenROAD, whose newest tag is `26Q3`. GraphQL can
    order by TAG_COMMIT_DATE and carry the date, so one query answers both.
    """
    owner, _, name = up_full.partition("/")
    q = ("query($o:String!,$n:String!,$k:Int!){repository(owner:$o,name:$n){"
         "refs(refPrefix:\"refs/tags/\",first:$k,"
         "orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){nodes{name target{"
         "...on Commit{committedDate} ...on Tag{target{...on Commit{committedDate}}}}}}}}")
    try:
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={q}",
             "-F", f"o={owner}", "-F", f"n={name}", "-F", f"k={limit}"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None                      # could not ask
    if r.returncode != 0:
        return None                      # could not ask
    try:
        nodes = (json.loads(r.stdout).get("data") or {}).get(
            "repository", {}).get("refs", {}).get("nodes") or []
    except (json.JSONDecodeError, AttributeError):
        return None                      # could not ask
    out = []
    for nd in nodes:
        tgt = nd.get("target") or {}
        # Lightweight tag → Commit directly; annotated tag → Tag wrapping a Commit.
        date = tgt.get("committedDate") or (
            (tgt.get("target") or {}).get("committedDate"))
        iso = _iso_date(date)
        if nd.get("name") and iso:
            out.append({"tag": nd["name"], "date": iso})
    return out


def _releases(up_full: str) -> list[dict] | None:
    """Upstream versions newest-first: [{tag, date}] — releases AND tags, merged.

    `None` = NEITHER source could be asked. `[]` = both answered, and there are
    no versions. The caller must not read the first as the second (#49).

    vibeic-eda#31. This used to return as soon as the RELEASE list was non-empty,
    so the tag fallback only ever fired for a project that had never published a
    release at all. A project that published releases once and then moved to tags
    stayed pinned to the stale list forever.

    OpenROAD is exactly that: one release, `v0.9.0-beta` from 2020-07-06, while
    it actually ships quarterly tags (26Q1/26Q2/26Q3 — our own binary is
    `26Q3-951-g92b079b47a`). The ledger reported `upstream_latest_release =
    v0.9.0-beta`, our pin was six years ahead of it, `behind_releases` was 0, and
    `assess_release` skipped the tool permanently. Nothing failed; the tool was
    simply absent from every report, which is indistinguishable from a tool with
    nothing to adopt. Same for OpenSTA (2020-09-14) and gtkwave (2020-07-21) —
    three of the twenty-one forked upstreams, including the place-and-route and
    timing engines.

    So both sources are read and merged on DATE. A release and a tag naming the
    same version collapse to one entry, with the release's date preferred because
    it is the publication date rather than the commit date.
    """
    merged: dict[str, str] = {}
    # The API's OWN prerelease flag, kept per tag. It is a fact the release record
    # states, not something inferred from a tag name — no "rc"/"beta" matching, which
    # would be a second proxy of exactly the kind this module is being cured of.
    # None (a tag with no release record) means UNKNOWN, not False.
    pre: dict[str, bool] = {}
    rel = gh(f"repos/{up_full}/releases?per_page=30")
    rel_answered = isinstance(rel, list)
    if rel_answered:
        for r in rel:
            tag = r.get("tag_name")
            if tag:
                merged[tag] = _iso_date(r.get("published_at"))
                pre[tag] = bool(r.get("prerelease"))

    tags = _tags_by_date(up_full)
    for t in (tags or []):
        merged.setdefault(t["tag"], t["date"])

    if not merged:
        # NEITHER SOURCE ANSWERED is not "this project has no versions", and
        # until #49 both arrived here as the same `[]`. A caller reading that
        # empty list concludes we are current. Now the two are told apart at
        # the source: the releases endpoint answered iff it returned a list, and
        # `_tags_by_date` returns None only when it could not be asked.
        if not rel_answered and tags is None:
            return None
        return []

    # Undated entries sort last rather than first: an unknown date must never
    # win the "latest" slot that drives the ancestry compare.
    return [{"tag": k, "date": (v or None), "prerelease": pre.get(k)}
            for k, v in sorted(merged.items(),
                               key=lambda kv: (kv[1] or "", kv[0]), reverse=True)]


# ── CONTAINMENT ─────────────────────────────────────────────────────────────
# "Behind by N releases" is a claim about CONTENT: N pieces of work upstream has
# that our pinned ref does not. Until 2026-08-01 it was computed by comparing a
# release's PUBLICATION DATE against our fork-point date, with a single
# all-or-nothing ancestry probe on the newest release in front of it. A date is a
# proxy for containment, and the four rows the published page carried on
# 2026-08-01 are the four ways the proxy diverges from the property:
#
#   * a tag pointing at EXACTLY our pinned commit — tagged the day after the
#     commit was authored, so `published_at > fork_point_date` and it counted;
#   * two tags on the SAME commit — one release, counted twice;
#   * a prerelease and its final release — one piece of work, counted twice;
#   * a tag on a RELEASE BRANCH that only ever merges the branch we track — the
#     merge commit is by construction dated after the content it wraps and is by
#     construction not an ancestor of anything on the merged-from side, so both
#     the date test and the ancestry test say "new" about a zero-byte delta.
#
# Worse than any of those: when the API errored the code took the SAME branch as
# a measured "not contained" and emitted a NUMBER. Nothing downstream could tell
# a fabricated measurement from a real one, which is the defect this block exists
# to remove — hence `undetermined_releases` and `behind_releases = None`.
#
# The rule, and no tool appears in it:
#
#   1. resolve every candidate to its TARGET COMMIT (peeling annotated tags);
#   2. collapse candidates that resolve to the same commit — identity of a
#      version is its commit, not its name;
#   3. per distinct commit, ask CONTAINMENT: is it an ancestor of our pin, or
#      does it change no file relative to the merge-base with our pin? Either
#      one means our tree already has that work;
#   4. collapse a candidate the API itself flags `prerelease` into any counted
#      final release that already contains it;
#   5. drop a candidate that is BEHIND US rather than ahead — see below;
#   6. anything unanswerable is UNDETERMINED and the count is null.
#
# No arithmetic below reads a date.
#
# STEP 5, AND WHY IT IS NOT THE DATE FILTER COMING BACK. "Behind by N releases"
# has always meant releases we could ADVANCE to. The old code bounded that set
# with `release_date > fork_point_date`; delete the date and the bound goes with
# it, and a project that cuts every release on its own maintenance branch starts
# counting its whole history — a 2017 release-candidate does carry commits our
# pin lacks (branch-only ones), so "not contained" alone says yes to it.
#
# The bound the graph itself supplies is the TRUNK DIVERGENCE POINT. For a
# release R let
#
#     t(R) = merge-base(R, our fork point)
#
# — the place on the shared trunk where R's line left it. Every t is a trunk
# commit, so ancestry among the t's is a real ordering of releases with no clock
# in it. A release is behind us when its line left the trunk STRICTLY EARLIER
# than the line of the newest release we actually contain:
#
#     R counts  ⟺  t(base) is an ancestor of t(R)
#
# where `base` is the newest release our pin contains, chosen by that same
# ordering. Measured on the two shapes that pin it down, and no other candidate
# rule survives both:
#
#   * maintenance releases of a superseded series — t is a PROPER ANCESTOR of
#     t(base), so they drop. (Testing "is our fork point an ancestor of R" also
#     drops them, but see the next case, which it gets wrong.)
#   * a patch release cut from the very release we build, while the trunk moved
#     on separately — t equals t(base), so it counts. Our fork point is NOT an
#     ancestor of it, and our pin does not contain it: it is the newest release
#     upstream has and we do not have it.
#
# It is a fact about ancestry, computed from ancestry. A release whose line left
# the trunk before ours is not "probably old", it is provably not something we
# can move up to.

#: WHERE the fork clones live. Containment is decided against these whenever they
#: can answer, which spends no GitHub API budget at all — one compare call per
#: release across 36 tools is a bill this program cannot afford daily.
FORK_CLONES = Path(os.environ.get("GK_FORK_CLONES")
                   or os.path.expanduser("~/vibe-ic-forks"))
#: How many API compares ONE tool may spend when no clone can answer. Candidates
#: past it are UNDETERMINED, never assumed: a stated budget that shows up in the
#: ledger, rather than a silent truncation that reads like a measurement.
API_PROBE_CAP = int(os.environ.get("GK_RELEASE_PROBE_CAP") or "25")

#: The dispositions. "We could not ask" is one of them, and it is not "no".
#: FOLDED is its own disposition rather than a flavour of CONTAINED: a prerelease
#: whose work is counted under the final release that supersedes it carries commits
#: our pin does NOT have, so filing it beside the releases we already build states
#: something false about our tree. It does not change the count either way; it
#: changes which sentence the ledger and the page are making (vibeic-eda#36).
CONTAINED, NEW, SUPERSEDED, UNDETERMINED = "contained", "new", "superseded", "undetermined"
FOLDED = "folded"
#: …and EQUIVALENT is the same split, at the site that introduced FOLDED.
#:
#: CONTAINED is a claim about our TREE: our pinned ref already holds this work, so
#: adopting the release moves no byte. Round 2's own invariant test states it as
#: "an ancestor of our pin, or a merge into our pin that changes nothing", and run
#: against the real corpus rather than a fixture it failed on TWO live rows —
#: yices2 `yices-2.7.0`, which conflicts on `doc/sphinx/source/conf.py`, and
#: cocotb `v1.5.0rc1`, which conflicts on `documentation/source/release_notes.rst`.
#: Both arrived through the patch-equivalence branch, which is gated on neither
#: half of that invariant.
#:
#: Their claim is a different one, and it is TRUE: every commit they have that our
#: pin lacks is patch-identical to a commit our pin carries — and our pin has since
#: changed the same files again, which is why merging them is not a no-op. That is
#: not "we already build this tree"; it is "we carry this work and have moved past
#: it". So it gets a name of its own.
#:
#: It counts as containment everywhere the COUNT and `base_release` are concerned
#: — `yices-2.7.0` is the release yices2 builds and must stay `base_release`. Only
#: the heading changes, which is the whole of what was wrong.
EQUIVALENT = "patch-equivalent"
#: The buckets that mean OUR PIN HOLDS THIS RELEASE. They are what `base_release`
#: may name and what anchors the trunk ordering — and they are read from the
#: verdict that SURVIVED the re-proof, never from the one that went into it.
IN_PIN_BUCKETS = (CONTAINED, EQUIVALENT)
#: Buckets whose rows are re-proved from the repository before they are filed,
#: and the claim each one makes. See `_verify_buckets`.
VERIFIED_BUCKETS = (CONTAINED, EQUIVALENT)
#: …and the buckets that cannot be re-proved until steps 4 and 5 have decided
#: them, re-proved by the same machinery immediately afterwards.
#:
#: WHY THIS LIST EXISTS. Round 3 re-proved `contained` and `patch-equivalent` and
#: nothing else. Measured over the 36 real clones with no network at all: 299
#: rows, 229 re-proved, 70 NOT — superseded 59, new 9, undetermined 2. SUPERSEDED
#: is the bucket that takes a release OUT of `behind_releases` on the strength of
#: `_carried_by` and `_ancestor`, and two published zeroes (iverilog, gtkwave)
#: rested entirely on it. Verifying the claims that put work INTO the ledger and
#: not the ones that take it out leaves the cheaper direction unguarded — the
#: direction in which a wrong answer looks like health.
#:
#: NEW is here too. Its claim is the negative one — "our pinned ref does not have
#: this" — and it is re-proved by requiring `_verify_contained` to agree that our
#: pin does not hold it.
LATE_VERIFIED_BUCKETS = (SUPERSEDED, FOLDED, NEW)
#: How many of OUR commits one patch-equivalence re-proof may hash before it gives
#: up. Past it the row is recorded UNVERIFIABLE with the number, never assumed
#: sound. Measured: the two real rows needed 341 and 2276, at 0.32s and 0.63s.
PATCHID_CAP = int(os.environ.get("GK_PATCHID_CAP") or "20000")

#: `behind_releases` is an int ONLY under this status. The other two carry None.
MEASURED, UNKNOWN, NOT_PROBED = "measured", "unknown", "not-probed"

#: THE SWEEP'S OWN THREE ANSWERS, and the third of them is what round 5 adds.
#:
#: A REFUTATION AND A NON-MEASUREMENT ARE NOT THE SAME EVENT, so they must not
#: share an exit status any more than they share a bucket. A refutation is our own
#: repository contradicting our own claim: a defect in this code or in the ledger,
#: reproducible, and it should stop a pipeline. A row nothing could measure is a
#: slow disk, a loaded host, a clone missing an object, an API budget — none of
#: them a contradiction, and filing them under "BUCKET INVARIANT VIOLATED" would
#: print a false sentence and turn the daily cron red on a transient.
#:
#: BUT EXIT 0 IS ALSO FALSE FOR THEM, and that is the shape this round exists to
#: remove. The measured defect ended `violations=[] -> main() exits 0` while the
#: ledger said the release we build could not be established; a sweep that reports
#: success while recording that it could not measure is a check that lies. So the
#: third outcome gets a third status: the run happened, nothing contradicts itself,
#: and at least one release was not decided. A caller can gate on `!= 0`, on
#: `== 1`, or ignore 2 deliberately — what it can no longer do is fail to notice.
#:
#: Measured on the live corpus the day this was written: 34 tools, every one
#: `measured`, zero undetermined rows — so a healthy sweep still exits 0 and this
#: status is not a permanent red.
EXIT_CLEAN, EXIT_REFUTED, EXIT_NOT_MEASURED = 0, 1, 2


#: `rc` when the command DID NOT RUN. Not an exit status, because nothing exited:
#: git never got as far as having an opinion. It is `None` so that the two tests
#: this module is full of keep meaning what they say — `rc != 0` still catches it
#: (nothing succeeded), while `rc == 1`, which is git's code for a CLEAN, DEFINITE
#: NO, stops firing on a failure.
DID_NOT_RUN = None


class Git(NamedTuple):
    """What one git command did. Unpacks as `(rc, out, err)` like a 3-tuple.

    THE DEFECT THIS TYPE EXISTS TO REMOVE, and it is round 1's defect one layer
    down. `_git` used to map every subprocess exception to `return 1, "", ...`,
    and 1 is also git's own exit code for a clean negative — "not an ancestor",
    "no merge base", "the merge conflicts". A `git merge-base` that TIMED OUT and
    a `git merge-base` that ran and said "these two commits share no history"
    arrived at every call site as the same three values, so callers stated the
    second sentence when the first had happened:

      * `_local_containment` announced "shares no ancestor with our pinned ref"
        and, with an anchor, step 5 turned that into SUPERSEDED — the release
        left the count while `behind_releases_status` stayed `measured`.
        Measured end-to-end through `discover_one` with nothing patched but a
        `git` on PATH that hangs on `merge-base`: a genuinely missing release
        went from `behind=1 measured` to `behind=0 measured`.
      * `_verify_contained` read the same 1 as a REFUTATION, so a timeout made a
        genuinely contained release a bucket violation: `behind=0 measured`
        became `behind=None unknown` and the sweep exited non-zero on a healthy
        repository.

    Severity was latent — the slowest real `merge-base` on the 36 clones is
    0.126 s against a 60 s timeout — and latent is not the same as absent: it is
    one slow disk, one loaded host or one 300 s `merge-tree` away, and it fails
    in the direction that removes work from the count.

    A failed command and a clean negative are DIFFERENT FACTS and are now
    different values. `ran` is the question every caller has to answer before it
    is allowed to have a verdict.
    """
    rc: int | None
    out: str
    err: str

    @property
    def ran(self) -> bool:
        """Did git execute and exit? False means there is no answer here at all."""
        return self.rc is not None

    @property
    def ok(self) -> bool:
        """git ran and said YES (exit 0)."""
        return self.rc == 0

    @property
    def said_no(self) -> bool:
        """git ran and said NO — its exit code 1, a measurement, not a failure."""
        return self.rc == 1


def _git(repo, *args, stdin: str | None = None, timeout: int = 60) -> Git:
    """What `git <args>` in `repo` did. Never raises; see `Git`."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return Git(DID_NOT_RUN, "", f"{e.__class__.__name__}: {e}")
    return Git(r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip())


def _exit_phrase(rc: int | None) -> str:
    """How a command ended, in a sentence that cannot be mistaken for a verdict.

    A SIGNAL GIVES A NEGATIVE RETURNCODE. `subprocess` reports a process the
    kernel killed with signal N as `-N`, so a SIGKILLed git arrives as `rc ==
    -9`: not 0, not 1, and not any exit code git itself can produce. Every
    `rc != 0` in this module therefore covers two different events — git refused,
    and git was destroyed — and a message that guesses between them (the module
    used to answer a killed `merge-tree` with "git merge-tree --write-tree is
    unavailable (git < 2.38)") sends the reader after the wrong fault.
    """
    if rc is None:
        return "it never ran"
    if rc < 0:
        return f"killed by signal {-rc}"
    return f"exit {rc}"


class Probe(NamedTuple):
    """What ONE containment prover concluded — and whether it got to conclude.

    THE DEFECT THIS TYPE EXISTS TO REMOVE, and it is the same defect as `Git`'s,
    one layer further out. Round 4 made `_git` tri-state, so a subprocess that
    never ran stopped arriving as git's exit code 1. That value was then
    propagated CORRECTLY into `_merge_changes_nothing`, which answered None — and
    `_local_containment` read None the way it read False and FELL THROUGH TO NEW.

    NEW is not silence. It is an assertion: "this release carries commits and file
    changes our pinned ref does not have". Measured end to end through
    `discover_one`, no production module patched, a `git` on PATH that never
    returns from `merge-tree`, on a release our pin genuinely contains (its
    three-way merge writes exactly the tree we already build, and no other test
    can see that):

        control  behind=0 measured  base=v1.0  contained=[v1.0,v0.9]  violations=[]
        shim     behind=1 measured  base=v0.9  new=[v1.0]
                 unverifiable=[(v1.0,new,"merge-tree --write-tree did not run")]
                 violations=[]   ->  main() exits 0

    `behind_releases_status` still read `measured`; the row landed only in
    `unverifiable`, which nothing refuses on; and `base_release` — the release we
    build — moved. A hung subprocess changed the release we build, and the sweep
    exited 0.

    SO A PROVER NOW REPORTS TWO THINGS, and the caller has to read both. `value`
    is True (proved), False (ran, and the evidence says NO) or None (no verdict).
    `ran` separates the two ways to have no verdict, which is the rule this round
    installs: NO RESULT ("git never answered") and NO DATA ("git answered, and
    what it can see does not decide this") must never be the same value. Only a
    prover that RAN may contribute to the negative verdict NEW; one that did not
    contributes UNDETERMINED, which asserts nothing at all.
    """
    value: bool | None
    ran: bool
    why: str = ""

    @property
    def proved(self) -> bool:
        """It ran and it PROVED the property. The only value that may be relied on."""
        return self.value is True

    @property
    def refuted(self) -> bool:
        """It ran and the evidence says NO — a measurement, not a failure."""
        return self.value is False


def _no_result(why: str) -> Probe:
    """A prover that did not run. Contributes to no verdict in either direction."""
    return Probe(None, False, why)


def _no_data(why: str) -> Probe:
    """A prover that ran and cannot decide this input. It has still measured
    something, and the caller may fall through to a prover that can see more."""
    return Probe(None, True, why)


class Pipe(NamedTuple):
    """What `a | b` did — EVERY stage's exit status, not just the last one.

    A SHELL PIPELINE'S EXIT STATUS IS ITS LAST COMMAND'S, and that is how a
    producer that failed became an empty success. `_patch_id_set` ran

        git log -p --no-merges --format='commit %H' <rng> | git patch-id --stable

    under `shell=True` and screened it with `if r.returncode != 0: return None`.
    `git patch-id` exits 0 on empty input, so the screen COULD NOT SEE the
    producer fail. Measured with no shim and no monkeypatch, by deleting one blob
    object from a real clone — which is how clones actually break:

        git log -p alone         -> rc=128  fatal: unable to read <blob>
        THE PIPELINE AS WRITTEN  -> rc=0    stdout=''
        _patch_id_set            -> set()          (None would be honest)
        _verify_patch_equivalent -> (True, "all 0 of its commits are patch-identical
                                            to ones we carry")
        _verify_carried_by       -> (True, ...)

    `_verify_carried_by` is the re-proof round 4 added to close its own finding
    that the verification layer checked what ADDS to the count and not what
    REMOVES from it. So the re-proof of a removal was satisfiable by a command
    that failed: round 2's defect (`all([])` as proof) and round 4's defect (a
    command that did not run read as a clean result) in one expression, inside
    the code written to remove both.

    `rc` is one entry per stage, `None` for a stage that never ran at all. `ran`
    and `ok` are the two questions a caller has to answer before it is allowed a
    verdict, and neither of them can be answered by the last stage alone.
    """
    rc: tuple[int | None, ...]
    out: str
    err: str

    @property
    def ran(self) -> bool:
        """Did EVERY stage execute and exit?"""
        return bool(self.rc) and all(c is not None for c in self.rc)

    @property
    def ok(self) -> bool:
        """Did every stage exit 0? A producer that failed makes this False."""
        return bool(self.rc) and all(c == 0 for c in self.rc)


def _pipe(*stages: list[str], timeout: int = 900) -> Pipe:
    """Run `stages[0] | stages[1] | …` with NO SHELL, and report EVERY status.

    WHY NOT `set -o pipefail`, AND WHY NOT `PIPESTATUS`. Both work, and both keep
    a shell in a place that has no other use for one. `pipefail` is not in POSIX
    `sh` — the interpreter `shell=True` actually uses is `/bin/sh`, which on this
    host is `dash` — so it would have to come with `executable="/bin/bash"`, i.e.
    a second dependency added to make the first one safe. `PIPESTATUS` is bash
    only, for the same reason. Dropping the shell removes the failure mode rather
    than reporting it: with no shell there is no aggregate status to mistake for
    the producer's, and no quoting either, so `shlex.quote` on every interpolated
    path — the workaround a clone directory with a space in its name needed —
    goes away with it. The producer's stderr is kept, because "unable to read
    <blob>" is the sentence that says which object the clone is missing.
    """
    procs: list[subprocess.Popen] = []
    # Every stage's stderr goes to ONE temporary FILE, never to a pipe nobody is
    # reading: a stage that blocks writing its diagnostics while the next stage
    # waits for its input is a deadlock, and the diagnostics are the whole reason
    # to keep them.
    with tempfile.TemporaryFile(mode="w+", errors="replace") as err:
        try:
            try:
                prev = None
                for argv in stages:
                    p = subprocess.Popen(
                        argv,
                        stdin=prev.stdout if prev is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=err, text=True)
                    if prev is not None:
                        # So an upstream stage sees EPIPE if a downstream one exits.
                        prev.stdout.close()
                    procs.append(p)
                    prev = p
            except OSError as e:
                for p in procs:
                    p.kill()
                    p.wait()
                return Pipe(tuple([None] * len(stages)), "",
                            f"{e.__class__.__name__}: {e}")
            out, _ = procs[-1].communicate(timeout=timeout)
            for p in procs[:-1]:
                p.wait(timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            for p in procs:
                p.kill()
                try:
                    p.wait(timeout=10)
                except subprocess.SubprocessError:
                    pass
            return Pipe(tuple([None] * len(stages)), "",
                        f"{e.__class__.__name__}: {e}")
        finally:
            for p in procs:
                if p.stdout is not None and not p.stdout.closed:
                    p.stdout.close()
        err.seek(0)
        return Pipe(tuple(p.returncode for p in procs), out or "",
                    (err.read() or "").strip())


def _clone_for(tool: str) -> Path | None:
    """The local clone that can answer for `tool`, or None."""
    p = FORK_CLONES / tool
    return p if (p / ".git").exists() or (p / "HEAD").is_file() else None


def _peel(repo, revs: list[str]) -> dict[str, str] | None:
    """{rev: commit sha} for the revs THIS clone holds — one subprocess for all.
    None when the question was never put to git.

    `cat-file --batch-check` prints one line per input IN ORDER and prints
    `<input> missing` for an object the clone does not have, so the same call
    answers both "what commit is this name" and "is that commit here". A rev the
    clone cannot resolve is simply absent from the result: the caller must then
    ask somebody who can, never assume.

    AN EMPTY DICT AND None ARE DIFFERENT ANSWERS. `{}` is "git looked and this
    clone does not hold them"; None is "git did not look". The callers that route
    on it — `local_ok`, the API fall-back — behave the same either way, but the
    two re-proofs state a REASON on the row they decline to verify, and
    "the release commit is not in this clone" is a claim about the clone that a
    `cat-file` which never ran has not earned.
    """
    if not revs:
        return {}
    r = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
             "--buffer", stdin="".join(f"{rev}^{{commit}}\n" for rev in revs))
    if not r.ran:
        return None
    if r.rc != 0 and not r.out:
        return {}
    lines = r.out.splitlines()
    got = {}
    for rev, line in zip(revs, lines):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit" and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            got[rev] = parts[0]
    return got


def _range_counts(repo, base_sha: str, head_sha: str):
    """(commits in `base..head`, of which non-merge) — or None if git would not say.

    The SIZE of the range `git cherry` is about to be asked to summarise, measured
    separately from `git cherry`, because `git cherry` cannot report what it did
    not look at.
    """
    a = _git(repo, "rev-list", "--count", f"{base_sha}..{head_sha}", timeout=180)
    n = _git(repo, "rev-list", "--no-merges", "--count",
             f"{base_sha}..{head_sha}", timeout=180)
    if not a.ok or not n.ok or not a.out.isdigit() or not n.out.isdigit():
        return None
    return int(a.out), int(n.out)


def _patch_equivalent(repo, head_sha: str, base_sha: str):
    """Does every commit `head_sha` has and `base_sha` lacks already exist in
    `base_sha` under a DIFFERENT sha? A `Probe` — and the caller must read
    `.ran` as well as `.value`, because "git would not run" and "git ran and
    this walk cannot see the answer" are different facts with different
    consequences and used to be the same `None`.

    `git cherry <base> <head>` walks `base..head` and prints each commit prefixed
    `-` when a commit reachable from `base` (back to their merge-base) produces
    the IDENTICAL patch, `+` when nothing does. So "no `+` line" is exactly the
    property — OVER THE COMMITS IT WALKED, which is not the same set as the range.

    WHY ANCESTRY IS NOT ENOUGH. A release tag frequently sits on a version-stamp
    commit that upstream also merged to its trunk under a new sha — a squash
    merge, a backport, a rewritten release branch. The tagged commit is then not
    an ancestor of anything we build, its tree differs from the merge-base's, and
    every ancestry-shaped test says "work you do not have" about a change that is
    byte-for-byte already in our tree. `git patch-id --stable`, which
    `assess_release.already_carried` already uses for the same reason on the
    commit path, is what sees through it; `git cherry` is that comparison run
    over a whole range in one process.

    WHAT `git cherry` DOES NOT WALK, AND WHY THAT USED TO READ AS PROOF. `git
    cherry` walks with `max_parents = 1`, so a MERGE COMMIT in the range is never
    listed and never has its patch-id compared. The tail of this function used to
    be `all(ln.startswith('-') for ln in lines) or not lines`, and `all([])` is
    True: an EMPTY WALK WAS ACCEPTED AS PROOF OF CONTAINMENT — and
    `_local_containment` returns on it BEFORE `_merge_changes_nothing`, the one
    test that can see a merge, ever runs.

    Measured on a constructed repository driven through these very functions: a
    release tag on a merge commit whose own tree adds `cve.txt`, both of its
    parents ancestors of our pin, our pin without `cve.txt`. `rev-list
    <pin>..<rel>` = 1, `git cherry` = 0 lines, this function returned True, and
    the release was filed as work we already have.

    LATENT RATHER THAN LIVE, and counted rather than assumed: every tag in every
    pinned tool's clone — 2248 of them across the 29 tools that have both a pin
    and a clone — was tested for that exact shape (`rev-list --count pin..tag > 0`
    AND `rev-list --no-merges --count pin..tag == 0`). 0 matched, so no number
    published today is wrong. It is one release-time fixup away from being wrong:
    netgen tags 57 merge commits in its first 60 tags, sby 39, magic 38, yices2 9,
    klayout 5, cadical 4, and netgen's own `1.5.323` IS a merge commit — saved
    today only because the merge-base tree-equality test happens to fire in front
    of this one.

    SO THE RANGE IS MEASURED, NOT ASSUMED. `rev-list --count` and `rev-list
    --no-merges --count` say how many commits the range holds and how many of
    them `git cherry` was able to consider. When those disagree — or when the
    walk returned a different number of lines than the range holds — this
    function SAW LESS THAN THE WHOLE and answers None.

    TREATING ONLY THE EMPTY OUTPUT AS INCONCLUSIVE WOULD NOT HAVE BEEN ENOUGH,
    and that too is measured rather than argued: a range holding one ordinary
    patch-equivalent commit AND one such merge prints exactly one line, a `-`,
    the output is not empty, an empty-output guard never fires, and the evil
    merge is contained-by-assertion just as before.

    THE FAILURE DIRECTION THIS MUST NOT HAVE. A release we genuinely lack has at
    least one commit whose patch nothing of ours reproduces, so `git cherry`
    prints a `+` and the release stays counted. Anything that is not a clean run
    of git — a missing object, a timeout, a non-zero exit — has no verdict, and
    no verdict never means "contained" at the call site. Nor does an incomplete
    walk: a range this function could not see all of has no verdict either. There
    is no input for which a failure OR A BLIND SPOT of this function removes a
    release from the count.
    """
    counts = _range_counts(repo, base_sha, head_sha)
    if counts is None:
        return _no_result("git rev-list would not size the range")
    n_all, n_nomerge = counts
    if n_all != n_nomerge:
        # Merge commits in the range. `git cherry` will not walk them, so whatever
        # it prints is a statement about a strict subset of the work in question.
        # git RAN and said so: this is a blind spot of the walk, not a failure of
        # the machine, and the caller may go on to the one test that can see a
        # merge. It is `_no_data`, not `_no_result`.
        return _no_data(f"the range holds {n_all - n_nomerge} merge commit(s), which "
                        f"`git cherry` does not walk")
    walk = _git(repo, "cherry", base_sha, head_sha, timeout=180)
    if not walk.ok:
        return _no_result(f"git cherry did not answer (exit {walk.rc}): {walk.err[:100]}")
    lines = [ln for ln in walk.out.splitlines() if ln.strip()]
    if any(ln.startswith("+") for ln in lines):
        return Probe(False, True, "at least one of its commits reproduces no patch of ours")
    if len(lines) != n_nomerge:
        # The walk and the range disagree about how many commits there are. That
        # is not a "no": it is this function failing to account for the range.
        return _no_data(f"git cherry printed {len(lines)} line(s) for a range of "
                        f"{n_nomerge} commit(s)")
    # Every commit in the range was walked, and every one of them was `-`. An
    # empty range lands here too and is True for the right reason: there is
    # nothing in it that we lack.
    return Probe(all(ln.startswith("-") for ln in lines) or not lines, True,
                 f"git cherry accounted for all {n_nomerge} commit(s) in the range")


def _merge_changes_nothing(repo, head_sha: str, base_sha: str, mb: str):
    """Would merging `head_sha` into `base_sha` change base's tree at all?
    A `Probe`: proved (it would change nothing) / refuted / no verdict.

    This is the adoption question asked literally: `git merge-tree --write-tree`
    performs the real three-way merge the gatekeeper would perform and writes the
    resulting tree. When that tree IS the tree we already build, adopting the
    release moves no byte, and a release that moves no byte is not work we lack.

    It answers a shape `git cherry` cannot: upstream cutting a release branch,
    then REWRITING it, so the tagged commits are neither ancestors of ours nor
    patch-identical to ours, while the file states they produce are ones our
    trunk already reached by its own commits.

    Sound in the direction that matters: a release carrying anything we do not
    have contributes it to the merge, so the merged tree differs from ours and
    the release stays counted. A conflict is a non-zero exit and is NOT a
    no-op — it is reported as False here and the release stays counted. Git
    older than 2.38 has no `--write-tree`; that exits non-zero too, and the
    caller falls through to the next test rather than inventing an answer.

    A merge-tree that DID NOT RUN is not a conflict either. `rc == 1` is the
    exit code of a merge git performed and found conflicting; a merge git never
    performed has no exit code at all (`Git.ran`), and reporting it as False
    would be the same fold this round removes — here in the direction that keeps
    a release counted, which is not a reason to leave it.

    AND NOR IS IT A VERDICT OF ANY KIND. Every one of the three ways this ends
    without a merge is `_no_result`: no merge was performed, so nothing about
    this release was measured, and the caller may not fall through to NEW on the
    strength of it. That fall-through is what round 5 removes; see `Probe`.
    """
    r = _git(repo, "merge-tree", "--write-tree", f"--merge-base={mb}",
             base_sha, head_sha, timeout=180)
    if not r.ran:                        # git did not run: no merge, no verdict
        return _no_result(f"merge-tree --write-tree did not run: {r.err[:100]}")
    if r.said_no:
        return Probe(False, True, "merging it conflicts, so adopting it is not a no-op")
    first = (r.out.splitlines() or [""])[0].strip()
    if r.rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", first):
        # A merge that produced no tree. That is git < 2.38 without `--write-tree`,
        # a bad object, or a git the kernel killed — `rc` is NEGATIVE for the last
        # one, which is why this may not be written as `rc == 1` or `rc != 0` and
        # left to speak for git's version. Say the exit status and let the reader
        # tell them apart.
        return _no_result(f"merge-tree --write-tree produced no tree "
                          f"({_exit_phrase(r.rc)}): {r.err[:100]}")
    trees = _tree_pair(repo, base_sha, base_sha)
    if trees is None:
        return _no_result("the tree of our pinned ref could not be read")
    return Probe(first == trees[0], True,
                 "the merged tree is the tree we already build" if first == trees[0]
                 else "the merged tree is not the tree we already build")


def _local_containment(repo, tag_sha: str, pin_sha: str):
    """(verdict, why, disjoint) for one release commit against our pin, locally.

    FOUR questions, in cost order, and every one of them is about CONTENT:

      ancestry — `merge-base --is-ancestor` — is a sufficient shortcut, never the
      verdict. A tag placed on a merge commit that re-joins history we already
      have is not an ancestor of anything on the merged-from side while
      contributing nothing, so a NO here means "keep asking", not "behind".

      trees — the tag's tree against the tree of the merge-base with our pin. The
      merge-base is by definition reachable from our pin, so an equal tree means
      the tag's content is content we already build. This is the same question
      the API answers as "zero changed files".

      patch-equivalence — every commit the tag has and we lack is byte-identical,
      as a patch, to a commit we carry (`git cherry` over `git patch-id
      --stable`). Ancestry cannot see a change that reached our trunk under a new
      sha; this can. Measured on a release whose one missing commit was the
      version stamp for that very release, squash-merged to trunk beforehand:
      identical patch-id, different sha, and our own header already declaring the
      released version. Its verdict is EQUIVALENT rather than CONTAINED: the work
      is ours, the tree is not necessarily, and only one of those two sentences is
      what `contained` says.

      merge — the three-way merge of the release into our pin produces the tree
      we already build. That is the adoption question asked literally, and it is
      the one that survives an upstream REWRITING its release branch, where
      neither ancestry nor patch-id can match but the file states are ours
      already.

    None of the four reads a date, and none of them names a project.

    `disjoint` says the release and our pin share NO ancestor at all. That is a
    definite answer from git, not a failure, and it means something the other
    values cannot express: the release is not on the line we track, so it is not
    a release we could advance to by any rebase. Measured on an upstream that
    re-imported its history — its newest release is a 115-commit tree with a
    different root commit from the 3459-commit history its older tags sit on.

    Returns (None, reason, False) when this clone cannot decide — a missing
    object, a git that would not run.

    AND THAT INCLUDES RUNNING OUT OF QUESTIONS. NEW is the last line of this
    function and it used to be unconditional, which made the ABSENCE OF A PROOF
    into a POSITIVE CLAIM: "carries commits and file changes our pinned ref does
    not have" is an assertion about the release, and a `merge-tree` that never ran
    is not evidence for it. The two remaining tests are asked as `Probe`s, and NEW
    is spoken only when both of them RAN and both said no. If either produced no
    result the answer is None — the release is undetermined, this tool's count and
    `base_release` are withheld, and the sweep says so in its exit status. Measured
    with a `git` on PATH that never returns from `merge-tree`, on a release whose
    three-way merge writes exactly the tree we build: `behind=0 measured
    base=v1.0` became `behind=1 measured base=v0.9 new=[v1.0]`.

    `disjoint` IS A MEASUREMENT AND MUST COME FROM ONE. It used to be reached by
    `if rc != 0 or not mb`, which is true of a `git merge-base` that timed out
    (`_git` returned 1 for every exception), of one killed by the OOM killer, and
    of `rc == 128` — a bad object, git RUNNING and REFUSING. Any of those then
    said "shares no ancestor with our pinned ref", and step 5 turns that into
    SUPERSEDED as soon as we contain any release at all, so the release left the
    count with `behind_releases_status` still reading `measured`. Measured
    end-to-end through `discover_one`, no production code patched, a `git` on
    PATH that hangs on `merge-base`: `behind=1 measured` became `behind=0
    measured`. Only git's own exit 1 — and an exit 0 with nothing printed — is
    the clean "these two share no history".
    """
    anc = _git(repo, "merge-base", "--is-ancestor", tag_sha, pin_sha)
    if not anc.ran:
        return None, f"git merge-base --is-ancestor did not run: {anc.err[:120]}", False
    if anc.ok:
        return CONTAINED, "ancestor of our pinned ref", False
    if not anc.said_no:                      # 1 = a clean "no"; anything else is a failure
        return None, f"git merge-base --is-ancestor failed: {anc.err[:120]}", False
    mbr = _git(repo, "merge-base", tag_sha, pin_sha)
    if not mbr.ran:
        return None, f"git merge-base did not run: {mbr.err[:120]}", False
    mb = mbr.out
    if mbr.said_no or (mbr.ok and not mb):
        # git looked and there is no shared history at all. The trees are the
        # only remaining question, and answering it is still a measurement.
        trees = _tree_pair(repo, tag_sha, pin_sha)
        if trees is None:
            return None, "could not read the trees of the release and our pinned ref", False
        if trees[0] == trees[1]:
            return CONTAINED, "identical tree to our pinned ref", False
        return NEW, "shares no ancestor with our pinned ref", True
    if not mbr.ok:
        return None, f"git merge-base failed: {mbr.err[:120]}", False
    trees = _tree_pair(repo, tag_sha, mb)
    if trees is None:
        return None, "could not read the trees of the release and the merge-base", False
    if trees[0] == trees[1]:
        return (CONTAINED,
                "changes no file relative to the merge-base with our pinned ref", False)
    pe = _patch_equivalent(repo, tag_sha, pin_sha)
    if pe.proved:
        # EQUIVALENT, not CONTAINED — see the constant. Our pin carries the work;
        # our pin is not the same tree, and may not even merge it cleanly.
        return (EQUIVALENT,
                "every commit it has that our pinned ref lacks is patch-identical "
                "(git patch-id --stable) to a commit our pinned ref already carries, "
                "and our pinned ref has since moved on from it", False)
    mn = _merge_changes_nothing(repo, tag_sha, pin_sha, mb)
    if mn.proved:
        return (CONTAINED,
                "merging it into our pinned ref produces the tree we already build — "
                "it changes no file", False)
    # THE DEFAULT IS THE DEFECT, and this is where it was. Reaching the end of the
    # list without a proof used to RETURN NEW — and NEW is not the absence of a
    # claim, it is the claim "this release carries commits and file changes our
    # pinned ref does not have". A prover that never ran had just been made to say
    # so honestly (`Probe.ran`, round 4's `Git.ran` one layer out) and the honesty
    # was thrown away one line later.
    #
    # NEW is now spoken only when EVERY remaining prover RAN and every one of them
    # said no. When one did not run there is no negative to state: the release is
    # UNDETERMINED, which asserts nothing, nulls this tool's count, withholds
    # `base_release`, and — since round 5 — makes the sweep exit 2 rather than 0.
    #
    # `_no_data` is deliberately NOT `_no_result` here: a `git cherry` that cannot
    # walk a merge has still measured the range, and the merge test that CAN see a
    # merge has just answered. Only a prover that produced nothing at all suppresses
    # the verdict, which is why the round-2 evil-merge shape still counts.
    if not (pe.ran and mn.ran):
        stalled = [p.why for p in (pe, mn) if not p.ran]
        return (None,
                "no test could show our pinned ref already holds it, and the tests that "
                "would have decided it did not run, so whether we have it was never "
                "measured: " + "; ".join(stalled), False)
    return NEW, "carries commits and file changes our pinned ref does not have", False


def _verify_contained(repo, tag_sha: str, pin_sha: str):
    """Re-prove CONTAINED from the repository. (True / False / None, why).

    THE INVARIANT `contained_releases` ASSERTS, stated once and checked where the
    rows are actually produced. Four proofs, any one of which makes "our pinned
    ref already holds this" a true sentence:

      * the release is an ancestor of our pin;
      * its tree IS our pin's tree;
      * its tree is the tree of the merge-base with our pin, so it adds nothing
        to the line we share;
      * the three-way merge of it into our pin produces the tree we already build.

    WHY THIS EXISTS AT ALL, given `_local_containment` just decided the same
    thing. Round 2 wrote this invariant as a test — and the only input the test
    ever saw was a fixture built to satisfy it. Run against the real ledger it
    failed on two live rows the day it was written. A check that runs only in a
    fixture is not a check on production; this one runs on every sweep, over the
    rows the sweep is about to file.

    It is deliberately NOT the same set of questions `_local_containment` asks.
    It never calls `_patch_equivalent`, so a bug in the patch path cannot make
    its own verification pass — which is exactly the bug this round is closing.

    None means the proof could not be RUN (a missing object, a git without
    `merge-tree --write-tree`). None is not a violation and never becomes one:
    it is recorded as unverifiable, with the reason, and the row stands.

    WHICH IS EXACTLY WHY NO COMMAND THAT DID NOT RUN MAY RETURN False HERE. False
    is a REFUTATION: it nulls the row's verdict, files it under
    `undetermined_releases`, nulls the tool's count and exits the sweep non-zero.
    Two sites used to hand that verdict to a git that never answered — `rc == 1`
    from a timed-out `merge-base`, and `rc == 1` from a timed-out `merge-tree`,
    both of them indistinguishable from git's clean "no shared history" and
    "this merge conflicts". Measured through `discover_one` with a `git` on PATH
    that hangs on the re-proof's own `merge-base`: a genuinely contained release
    went from `behind=0 measured` to `behind=None unknown`, and the sweep exited
    non-zero on a repository with nothing wrong with it.
    """
    have = _peel(repo, [tag_sha, pin_sha])
    if have is None:
        return None, "git could not be asked whether this clone holds the release commit"
    if not have.get(tag_sha):
        return None, "the release commit is not in this clone"
    anc = _git(repo, "merge-base", "--is-ancestor", tag_sha, pin_sha)
    if not anc.ran:
        return None, f"merge-base --is-ancestor did not run: {anc.err[:100]}"
    if anc.ok:
        return True, "ancestor of our pinned ref"
    if not anc.said_no:
        return None, f"merge-base --is-ancestor failed: {anc.err[:100]}"
    trees = _tree_pair(repo, tag_sha, pin_sha)
    if trees is None:
        return None, "could not read the trees of the release and our pinned ref"
    if trees[0] == trees[1]:
        return True, "identical tree to our pinned ref"
    mbr = _git(repo, "merge-base", tag_sha, pin_sha)
    if not mbr.ran:
        return None, f"merge-base did not run: {mbr.err[:100]}"
    mb = mbr.out
    if mbr.said_no or (mbr.ok and not mb):
        return False, "shares no ancestor with our pinned ref, and its tree is not ours"
    if not mbr.ok:
        return None, f"merge-base failed: {mbr.err[:100]}"
    mtrees = _tree_pair(repo, tag_sha, mb)
    if mtrees is not None and mtrees[0] == mtrees[1]:
        return True, "changes no file relative to the merge-base with our pinned ref"
    mt = _git(repo, "merge-tree", "--write-tree", f"--merge-base={mb}",
              pin_sha, tag_sha, timeout=300)
    if not mt.ran:
        return None, f"merge-tree --write-tree did not run: {mt.err[:100]}"
    if mt.said_no:
        conflicted = [ln for ln in mt.out.splitlines() if ln.startswith("CONFLICT")]
        return False, ("merging it into our pinned ref CONFLICTS: "
                       + (conflicted[0][:160] if conflicted else "unresolved"))
    first = (mt.out.splitlines() or [""])[0].strip()
    if not mt.ok or not re.fullmatch(r"[0-9a-f]{40}", first):
        # NOT "git < 2.38". That was the only cause this line named, and a git the
        # kernel killed lands here too — `rc == -9`, which is neither 0 nor 1 nor
        # any exit code git can produce. Naming one cause for a condition that has
        # three sends the reader after a version that is not the problem; measured
        # with a `git` on PATH that SIGKILLs itself on `merge-tree`, which reported
        # "git merge-tree --write-tree is unavailable (git < 2.38)" on git 2.43.
        return None, (f"git merge-tree --write-tree produced no tree "
                      f"({_exit_phrase(mt.rc)}): {mt.err[:100] or 'no diagnostics'}")
    if first == trees[1]:
        return True, "merging it into our pinned ref changes no file"
    return False, "merging it into our pinned ref changes files our pinned ref does not have"


def _patch_id_set(repo, rng: str, cap: int):
    """{patch-id} for the non-merge commits in `rng` — or None.

    `git log -p | git patch-id --stable` is the same normalisation `git cherry`
    performs internally, run here WITHOUT `git cherry`, so this can disagree with
    it. One process pair for the whole range: measured 0.63s over cocotb's 2276
    commits.

    AN EMPTY SET AND None ARE DIFFERENT ANSWERS, and until round 5 a failure
    produced the first. The two processes ran under `shell=True` and the result
    was screened with `if r.returncode != 0`, which is the SHELL's status, which
    is the LAST command's — and `git patch-id` exits 0 on empty input. So a
    `git log -p` that died on a missing blob came back as a clean, empty answer,
    and an empty answer is what both callers read as PROOF: `theirs - ours` is
    empty for every `ours`, so `_verify_patch_equivalent` and `_verify_carried_by`
    each answered "all 0 of its commits are patch-identical to ones we carry".
    Measured on a real clone with one blob deleted; see `Pipe`. `_pipe` reports
    every stage's status, so a producer that failed is now a `None` — "we could
    not find out" — which is what it always was.
    """
    cnt = _git(repo, "rev-list", "--count", "--no-merges", rng, timeout=300)
    if not cnt.ok or not cnt.out.isdigit():
        return None
    if int(cnt.out) > cap:
        return None
    # No shell: `git patch-id` reads the diff stream `git log -p` writes and there
    # is no plumbing form that does both, but a pipe does not need an interpreter.
    # Without one there is no aggregate status to mistake for the producer's, and
    # nothing to quote — `--format=commit %H` is one argv entry, not a shell word.
    p = _pipe(["git", "-C", str(repo), "log", "-p", "--no-merges",
               "--format=commit %H", rng],
              ["git", "patch-id", "--stable"], timeout=900)
    if not p.ok:
        return None
    return {ln.split()[0] for ln in (p.out or "").splitlines() if ln.strip()}


def _verify_patch_equivalent(repo, tag_sha: str, pin_sha: str):
    """Re-prove EQUIVALENT from the repository. (True / False / None, why).

    Splitting a bucket out and then not checking it moves an unverified claim
    rather than removing one, so the new bucket gets the same treatment as the
    old — and by an implementation that does not go through `git cherry`, whose
    blind spot is the whole subject of this round.

    It is therefore a SECOND, independent detector of that blind spot: a range
    holding a merge commit cannot have had every commit compared, and this says
    so directly rather than inferring it from what a walk printed.
    """
    have = _peel(repo, [tag_sha, pin_sha])
    if have is None:
        return None, "git could not be asked whether this clone holds the release commit"
    if not have.get(tag_sha):
        return None, "the release commit is not in this clone"
    counts = _range_counts(repo, pin_sha, tag_sha)
    if counts is None:
        return None, "could not size the range"
    n_all, n_nomerge = counts
    if n_all != n_nomerge:
        return False, (f"the range holds {n_all - n_nomerge} merge commit(s) whose own "
                       f"trees nothing compared — patch equivalence was claimed over a "
                       f"range that was never fully examined")
    theirs = _patch_id_set(repo, f"{pin_sha}..{tag_sha}", PATCHID_CAP)
    if theirs is None:
        return None, "git patch-id would not run over the release's own commits"
    ours = _patch_id_set(repo, f"{tag_sha}..{pin_sha}", PATCHID_CAP)
    if ours is None:
        return None, (f"our side of the range is larger than GK_PATCHID_CAP={PATCHID_CAP} "
                      f"or git patch-id would not run")
    missing = theirs - ours
    if missing:
        return False, f"{len(missing)} of its {len(theirs)} commit(s) match nothing we carry"
    return True, f"all {len(theirs)} of its commits are patch-identical to ones we carry"


def _verify_not_contained(repo, tag_sha: str, pin_sha: str):
    """Re-prove NEW. (True / False / None, why).

    NEW's claim is the negative one — our pinned ref does NOT already hold this
    release — so its re-proof is `_verify_contained` failing to find any of the
    four proofs. True here means "and it did not find one", which is the row
    standing; False means our own re-prover says we DO contain a release the
    classifier counted as missing, and that contradiction is not a measurement
    either.
    """
    ok, why = _verify_contained(repo, tag_sha, pin_sha)
    if ok is None:
        return None, why
    if ok is False:
        return True, f"our pinned ref does not hold it: {why}"
    return False, f"our pinned ref DOES hold it — {why} — so it is not a release we lack"


def _verify_carried_by(repo, tag_sha: str, by_sha: str, by_tag: str):
    """Re-prove "`by_sha` already carries every commit `tag_sha` has".

    The claim `_carried_by` makes, re-derived WITHOUT `git cherry`: ancestry
    first, then the patch-id set comparison `_verify_patch_equivalent` performs —
    which also refuses a range holding merge commits, because a range with a
    merge in it cannot have had every commit compared by anything that walks
    `max_parents=1`.
    """
    anc = _git(repo, "merge-base", "--is-ancestor", tag_sha, by_sha)
    if not anc.ran:
        return None, f"merge-base --is-ancestor did not run: {anc.err[:100]}"
    if anc.ok:
        return True, f"an ancestor of {by_tag}, which carries it"
    if not anc.said_no:
        return None, f"merge-base --is-ancestor failed: {anc.err[:100]}"
    ok, why = _verify_patch_equivalent(repo, tag_sha, by_sha)
    return ok, f"{why} (against {by_tag})"


def _verify_disjoint(repo, tag_sha: str, pin_sha: str):
    """Re-prove "this release shares no ancestor with the line we track".

    THE ONE THAT USED TO BE FREE. `_local_containment` reached this by way of
    `if rc != 0 or not mb`, so a `merge-base` that timed out asserted it. Here it
    is asserted only by a `merge-base` that RAN and found nothing, and a
    merge-base that exists refutes it outright.
    """
    mbr = _git(repo, "merge-base", tag_sha, pin_sha)
    if not mbr.ran:
        return None, f"merge-base did not run: {mbr.err[:100]}"
    if mbr.said_no or (mbr.ok and not mbr.out):
        return True, "git finds no merge-base between it and our pinned ref"
    if not mbr.ok:
        return None, f"merge-base failed: {mbr.err[:100]}"
    return False, (f"it DOES share history with our pinned ref — merge-base "
                   f"{mbr.out[:12]} — so it is not an abandoned line")


def _verify_trunk_order(repo, tag_sha: str, base_sha: str, fp: str | None):
    """Re-prove "its line left the upstream trunk BEFORE the line of the release
    we build left it" — the ordering step 5 drops a release on.

    Both trunk points are recomputed here from the clone rather than read out of
    `_t`'s cache, and the ordering is recomputed too. A row whose trunk points
    only the API could supply is unverifiable from the repository and says so.
    """
    if not fp:
        return None, "we have no fork point, so no trunk point can be recomputed"
    tp = {}
    for name, sha in (("this release", tag_sha), ("the release we build", base_sha)):
        r = _git(repo, "merge-base", sha, fp)
        if not r.ran:
            return None, f"merge-base for {name} did not run: {r.err[:80]}"
        if not r.ok or not re.fullmatch(r"[0-9a-f]{40}", r.out or ""):
            return None, f"the trunk point of {name} could not be recomputed from this clone"
        tp[name] = r.out
    t_g, t_base = tp["this release"], tp["the release we build"]
    anc = _git(repo, "merge-base", "--is-ancestor", t_base, t_g)
    if not anc.ran:
        return None, f"merge-base --is-ancestor did not run: {anc.err[:80]}"
    if anc.said_no:
        return True, (f"its line left the trunk at {t_g[:12]}, which is not a descendant "
                      f"of {t_base[:12]} where the line we build left it")
    if anc.ok:
        return False, (f"its trunk point {t_g[:12]} IS a descendant of {t_base[:12]} — "
                       f"it is not behind the release we build")
    return None, f"merge-base --is-ancestor failed: {anc.err[:80]}"


def _verify_removed(repo, g: dict, pin_sha: str, fp: str | None):
    """Re-prove a SUPERSEDED or FOLDED row from the BASIS step 4/5 recorded.

    Each of those steps drops a release out of `behind_releases` for one stated
    reason, and each reason is a question the repository can be asked again:

      carried-by  — the release we build (or the final release) already holds
                    every commit this one has;
      disjoint    — it shares no history with the line we track at all;
      trunk-order — its line left the upstream trunk before ours did.

    A row that reached one of those buckets WITHOUT recording its basis is
    unverifiable rather than assumed sound; there is no such path today, and if
    one is ever added the ledger will say so instead of going quiet.
    """
    basis = g.get("basis") or {}
    kind = basis.get("kind")
    if kind == "carried-by":
        return _verify_carried_by(repo, g["sha"], basis["sha"], basis.get("tag") or "it")
    if kind == "disjoint":
        return _verify_disjoint(repo, g["sha"], pin_sha)
    if kind == "trunk-order":
        return _verify_trunk_order(repo, g["sha"], basis["sha"], fp)
    return None, f"the row records no basis for {g.get('verdict')}, so there is nothing to re-prove"


def _verify_buckets(repo, groups: dict, pin_sha: str, buckets=VERIFIED_BUCKETS,
                    fp: str | None = None) -> dict:
    """Re-prove every verdict in `buckets`, and REFUSE the ones that do not
    survive. Returns what was checked, so a clean result over zero rows is
    visible as such rather than reading like a pass.

    A refuted row does not become NEW. Turning a self-contradiction into a
    measurement is the shape this whole module exists to remove: the classifier
    said one thing, an independent check of the repository says another, and the
    honest disposition for that release is that WE DO NOT KNOW. It goes to
    UNDETERMINED, which nulls the count for that tool and states the reason on
    the row.

    Called twice per tool, because the two families of verdict do not exist at
    the same moment: the containment buckets are decided in step 3 and re-proved
    before anything is filed in one; SUPERSEDED and FOLDED do not exist until
    steps 4 and 5 have run, and are re-proved the moment they do.
    """
    checked, unverifiable, violations = 0, [], []
    by_bucket: dict[str, int] = {}
    for g in groups.values():
        if g.get("verdict") not in buckets:
            continue
        claim = g["verdict"]
        if repo is None:
            ok, why = None, "no local clone holds both ends; decided over the API"
        elif claim == CONTAINED:
            ok, why = _verify_contained(repo, g["sha"], pin_sha)
        elif claim == EQUIVALENT:
            ok, why = _verify_patch_equivalent(repo, g["sha"], pin_sha)
        elif claim == NEW:
            ok, why = _verify_not_contained(repo, g["sha"], pin_sha)
        else:
            ok, why = _verify_removed(repo, g, pin_sha, fp)
        row = {"tag": g["tags"][0], "claim": claim, "reason": why}
        if ok is True:
            checked += 1
            by_bucket[claim] = by_bucket.get(claim, 0) + 1
        elif ok is None:
            unverifiable.append(row)
        else:
            violations.append({**row, "filed_as": g.get("why")})
            g["verdict"] = None
            # REFUTED IS A MEASUREMENT; UNDETERMINED-BECAUSE-NOTHING-RAN IS NOT.
            # Both null the verdict and both land in `undetermined_releases`, and
            # `base_release` must treat them differently: an independent check of
            # the repository has established that our pin does NOT hold this
            # release, so an older release may take its place as the one we build
            # (round 4's H2b constraint). A release nobody could measure may not be
            # stepped over that way — we do not know that it is not the one we
            # build. This flag is the difference, recorded where it is decided.
            g["refuted"] = True
            g["why"] = (f"classified {claim} — {g.get('why')} — but an independent check "
                        f"of the repository refutes it: {why}. A verdict that contradicts "
                        f"itself is not a measurement")
    return {"checked": checked, "by_bucket": by_bucket,
            "unverifiable": unverifiable, "violations": violations}


def _merge_checks(a: dict, b: dict) -> dict:
    """The two re-proof passes, as one record."""
    out = {"checked": a["checked"] + b["checked"],
           "by_bucket": dict(a.get("by_bucket") or {}),
           "unverifiable": a["unverifiable"] + b["unverifiable"],
           "violations": a["violations"] + b["violations"]}
    for k, v in (b.get("by_bucket") or {}).items():
        out["by_bucket"][k] = out["by_bucket"].get(k, 0) + v
    return out


def _tree_pair(repo, a: str, b: str):
    """(tree(a), tree(b)) or None if either could not be read."""
    r = _git(repo, "cat-file", "--batch-check=%(objectname)", "--buffer",
             stdin=f"{a}^{{tree}}\n{b}^{{tree}}\n")
    lines = r.out.splitlines()
    if not r.ok or len(lines) != 2:
        return None
    if not all(re.fullmatch(r"[0-9a-f]{40}", ln.strip()) for ln in lines):
        return None
    return lines[0].strip(), lines[1].strip()


def _api_containment(tool: str, up_full: str, tag_sha: str, pin_sha: str):
    """(verdict, why) for one release commit against our pin, over the API.

    ONE compare answers both halves: `compare/<pin>...<tag>` reports `ahead_by`
    (commits the tag has that our pin lacks) and `files` (what those commits
    change relative to the merge-base). `ahead_by == 0` is the ancestry
    shortcut; an empty `files` is the content test.

    RAW SHAS IN A SINGLE REPOSITORY, deliberately. The query this replaces was
    `compare/<upstream_owner>:<tag>...<ref>`, a CROSS-REPO form that resolves
    only through a shared fork network — and several of our mirrors are
    independent repositories (`fork: false`, `parent: null`), for which it 404s
    100% of the time. That 404 was then read as "not contained". Both endpoints
    are ordinary commits present in both repositories, so the same question is
    asked of whichever one answers, and an error from BOTH is an error, not a
    number.
    """
    errs = []
    for repo in (f"{ORG}/{tool}", up_full):
        c = gh(f"repos/{repo}/compare/{pin_sha}...{tag_sha}")
        if c.get("_err"):
            errs.append(f"{repo}: {c['_err']}")
            continue
        ahead = c.get("ahead_by")
        if not isinstance(ahead, int):
            errs.append(f"{repo}: compare returned no ahead_by")
            continue
        if ahead == 0:
            return CONTAINED, f"ancestor of our pinned ref (compare in {repo})", False
        files = c.get("files")
        if isinstance(files, list) and not files:
            return (CONTAINED,
                    f"changes no file relative to our pinned ref (compare in {repo})", False)
        if not isinstance(files, list):
            errs.append(f"{repo}: compare returned no file list")
            continue
        return NEW, f"{ahead} commit(s) and {len(files)} changed file(s) we do not have", False
    return None, "; ".join(errs) or "no compare could be run", False


def _ls_remote_tags(url: str) -> dict[str, str]:
    """{tag: commit sha} from ONE `git ls-remote` — no API budget, no clone.

    The peeled `refs/tags/<t>^{}` line wins over the plain one, which for an
    annotated tag names the tag OBJECT rather than the commit it points at.
    """
    try:
        r = subprocess.run(["git", "ls-remote", "--tags", url],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    plain, peeled = {}, {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/"):]
        (peeled if name.endswith("^{}") else plain)[name.removesuffix("^{}")] = parts[0]
    return {**plain, **peeled}


def classify_releases(tool: str, up_full: str, rels: list[dict], ref: str | None,
                      fork_point: str | None = None) -> dict:
    """WHICH upstream releases carry work our pinned ref does not have.

    Returns the whole answer, including the parts that could not be measured:

        {"new": [...], "contained": [...], "superseded": [...],
         "undetermined": [...], "behind": int | None,
         "status": MEASURED | UNKNOWN | NOT_PROBED,
         "base": tag | None, "api_calls": int, "clone": str | None}

    `behind` is an int ONLY under MEASURED. Under UNKNOWN it is None and
    `undetermined` names every release and the literal error that stopped it;
    under NOT_PROBED there was no pin or no upstream version to ask about, so the
    question has no subject. A caller that reads None as 0 has reintroduced the
    defect.

    `fork_point` is our pinned ref's merge-base with the upstream trunk. It is
    what bounds "ahead of us" without a date; when it is not known, a release
    that is not contained is UNDETERMINED rather than counted — not knowing where
    we branched from is not the same as knowing everything is ahead.
    """
    out = {"new": [], "contained": [], "equivalent": [], "superseded": [], "folded": [],
           "undetermined": [], "behind": None, "status": NOT_PROBED, "base": None,
           "api_calls": 0, "clone": None,
           "bucket_check": {"checked": 0, "by_bucket": {},
                            "unverifiable": [], "violations": []}}
    if not ref or not rels:
        return out

    clone = _clone_for(tool)
    out["clone"] = str(clone) if clone else None
    tags = [r["tag"] for r in rels]

    # 1. RESOLVE every candidate — and our pin — to a commit. Local clone first
    #    (free), then one `git ls-remote` per side (no API budget), then give up
    #    on the ones still unresolved. `pinned_ref_full` is not always a sha:
    #    ORFS pins the TAG `v3.0`, so the pin is peeled by the same machinery.
    # `_peel` answering None (git would not run) is not "the clone lacks them":
    # it means nothing was learned, and the remote is asked exactly as it is when
    # the clone genuinely lacks them.
    shas = (_peel(clone, tags + [ref]) if clone else {}) or {}
    if ref not in shas or any(t not in shas for t in tags):
        for url in (f"https://github.com/{up_full}.git", f"https://github.com/{ORG}/{tool}.git"):
            remote = _ls_remote_tags(url)
            for t in tags:
                if t not in shas and t in remote:
                    shas[t] = remote[t]
            if ref not in shas and ref in remote:
                shas[ref] = remote[ref]
            if ref in shas and all(t in shas for t in tags):
                break
    pin_sha = shas.get(ref) or (ref if re.fullmatch(r"[0-9a-f]{40}", ref or "") else None)
    if not pin_sha:
        out["status"] = UNKNOWN
        # `refuted` on EVERY undetermined row, including the two that are filed
        # before a verdict exists. The flag is what tells "an independent check of
        # the repository says this is not contained" from "nothing measured it",
        # and a row that omits it says neither — the shape this round removes.
        out["undetermined"] = [{**r, "sha": shas.get(r["tag"]), "refuted": False,
                                "error": f"our pinned ref {ref!r} could not be resolved "
                                         f"to a commit"} for r in rels]
        return out

    # 2. COLLAPSE by commit. Two names for one commit are one release — a bare
    #    `16.2.0` beside `trilinos-release-16-2-0`, a prerelease tag beside its
    #    own release record. Order is preserved so the first (newest) name leads.
    groups: dict[str, dict] = {}
    for r in rels:
        sha = shas.get(r["tag"])
        if not sha:
            out["undetermined"].append(
                {**r, "sha": None, "refuted": False,
                 "error": "the tag could not be resolved to a commit in the fork clone, "
                          "the upstream remote or our mirror"})
            continue
        g = groups.setdefault(sha, {"sha": sha, "tags": [], "date": r.get("date"),
                                    "prerelease": None})
        g["tags"].append(r["tag"])
        # A group is a prerelease only if a release record SAYS so and none says
        # otherwise. A tag with no release record contributes no opinion.
        if r.get("prerelease") is False:
            g["prerelease"] = False
        elif r.get("prerelease") is True and g["prerelease"] is not False:
            g["prerelease"] = True

    # 3. CONTAINMENT, per distinct commit. Local when the clone holds both ends,
    #    API otherwise, UNDETERMINED when neither can answer.
    local_ok = clone is not None and bool(_peel(clone, [pin_sha]))
    graph = clone if local_ok else None
    for g in groups.values():
        verdict = why = None
        disjoint = False
        if local_ok and _peel(clone, [g["sha"]]):
            verdict, why, disjoint = _local_containment(clone, g["sha"], pin_sha)
        if verdict is None:
            if out["api_calls"] >= API_PROBE_CAP:
                why = (f"probe budget exhausted after {API_PROBE_CAP} API compares "
                       f"(GK_RELEASE_PROBE_CAP)" + (f"; local: {why}" if why else ""))
            else:
                out["api_calls"] += 1
                api_verdict, api_why, disjoint = _api_containment(
                    tool, up_full, g["sha"], pin_sha)
                verdict, why = api_verdict, (api_why if api_verdict else
                                             f"{api_why}{'; local: ' + why if why else ''}")
        g["why"] = why
        g["verdict"] = verdict
        g["disjoint"] = disjoint

    # 3b. RE-PROVE THE BUCKETS BEFORE ANYTHING IS FILED IN ONE. Round 2 wrote this
    #     invariant as a test and the only input it ever saw was a fixture built to
    #     satisfy it; run against the real ledger it failed on two live rows. It
    #     runs here, on every sweep, over the rows this sweep is about to file, and
    #     a row it refutes is held UNDETERMINED rather than published.
    out["bucket_check"] = _verify_buckets(graph, groups, pin_sha)

    # 3c. …AND EVERYTHING DOWNSTREAM READS THE VERDICT THAT SURVIVED IT.
    #
    #     `in_pin` used to be assigned in the loop above, from the verdict as
    #     CLASSIFIED — before 3b ran. `_verify_buckets` refutes a row by nulling
    #     its verdict, and nothing cleared `in_pin`, so a row the sweep had just
    #     REFUSED was still the newest release our pin was held to contain: it was
    #     published as `base_release` and it became `ref_t`, the trunk anchor every
    #     other release's SUPERSEDED/NEW decision is ordered against.
    #
    #     Measured, on the fixture that plants a refutable claim in the production
    #     path: `undetermined_releases = [v2.0]`, `bucket_check.violations = [v2.0]`,
    #     `base_release = v2.0`. One file said "we cannot verify that we contain
    #     v2.0" and "the release we build is v2.0".
    #
    #     WHY it is contained still matters for picking `base_release`: only a
    #     release our PIN contains is a release we build. A prerelease folded into
    #     its final one (step 4) is contained for COUNTING purposes and is not a
    #     candidate to name as the version we ship.
    #
    #     EQUIVALENT counts here. `yices-2.7.0` is patch-equivalent rather than an
    #     ancestor, and it IS the release yices2 builds; excluding it would move
    #     that row's `base_release` back to `Yices-2.6.4` and its count from 0 to
    #     2 — measured, and the reason the merge test does not simply gate this.
    for g in groups.values():
        g["in_pin"] = g["verdict"] in IN_PIN_BUCKETS

    # 4. COLLAPSE a prerelease into a final release that already carries its work.
    #    The `prerelease` flag is the API's own; the containment is ancestry OR
    #    patch-equivalence. Neither half looks at the tag TEXT, so this is not an
    #    "rc" matcher.
    #
    #    ANCESTRY ALONE WAS NOT ENOUGH, measured: an upstream that rewrites its
    #    release branch between the candidate and the final leaves the candidate
    #    an ancestor of nothing, while three of its four commits exist in the
    #    final under new shas with identical patch-ids. Counting it beside the
    #    final states two releases' worth of missing work for one release's worth
    #    of commits — the same double-count as two tags on one commit, arrived at
    #    by a different route.
    #
    #    And the disposition is FOLDED, not CONTAINED. Its work IS counted, under
    #    the final; but our pinned ref does not contain it, and a bucket named
    #    `contained` would say that it does.
    finals = [g for g in groups.values()
              if g["verdict"] == NEW and g["prerelease"] is not True]
    for g in groups.values():
        if g["verdict"] != NEW or g["prerelease"] is not True:
            continue
        for f in finals:
            if _carried_by(graph, tool, up_full, g["sha"], f["sha"], out) is True:
                g["verdict"] = FOLDED
                g["folded_into"] = f["tags"][0]
                # WHAT THIS ROW RESTS ON, recorded so step 5b can ask the
                # repository the same question again by a different route.
                g["basis"] = {"kind": "carried-by", "sha": f["sha"], "tag": f["tags"][0]}
                g["why"] = (f"prerelease whose work {f['tags'][0]} already carries; counted "
                            f"once, under {f['tags'][0]}, not contained in our pinned ref")
                break

    # 5. BEHIND US, NOT AHEAD — the trunk-divergence ordering described above.
    #    t(R) = merge-base(R, our fork point) is the point on the shared trunk
    #    where R's line left it; a release counts only when t(base) is an ancestor
    #    of t(R), i.e. its line left the trunk no earlier than the line of the
    #    newest release we actually contain.
    fp = ((_peel(clone, [fork_point]) or {}).get(fork_point) if (clone and fork_point)
          else fork_point)
    tpoint: dict[str, str | None] = {}

    def _t(sha: str):
        if sha not in tpoint:
            tpoint[sha] = _merge_base(graph, tool, up_full, sha, fp, out) if fp else None
        return tpoint[sha]

    # `base` — the newest release our PIN contains, chosen by the same ordering
    # rather than by list position, which is a date order. Ties (several releases
    # off one trunk point) are broken by ancestry between the releases themselves,
    # so a patch release wins over the release it patches.
    base_g = None
    for g in groups.values():
        if not g.get("in_pin"):
            continue
        if base_g is None:
            base_g = g
            continue
        tb, tg = _t(base_g["sha"]), _t(g["sha"])
        if tb is None or tg is None:
            continue
        if tb != tg:
            if _ancestor(graph, tool, up_full, tb, tg, out) is True:
                base_g = g
        elif _ancestor(graph, tool, up_full, base_g["sha"], g["sha"], out) is True:
            base_g = g
    ref_t = _t(base_g["sha"]) if base_g is not None else fp

    for g in groups.values():
        if g["verdict"] != NEW:
            continue
        # ALREADY CARRIED BY THE RELEASE WE BUILD. If the release we were measured
        # to contain carries this candidate's work — ancestry or patch-equivalence,
        # the same two tests used everywhere else — then there is nothing here to
        # advance to: the thing that supersedes it is the thing we already build.
        # Measured on a prerelease of the very release our pin contains, which was
        # otherwise reported as "one release behind" on a row whose base release
        # and upstream latest release were the SAME tag.
        if (base_g is not None and g is not base_g
                and _carried_by(graph, tool, up_full, g["sha"], base_g["sha"], out) is True):
            g["verdict"] = SUPERSEDED
            g["basis"] = {"kind": "carried-by", "sha": base_g["sha"],
                          "tag": base_g["tags"][0]}
            g["why"] = (f"its work is carried by {base_g['tags'][0]}, which our pinned ref "
                        f"already contains — there is nothing here to advance to")
            continue
        # A release that shares NO ancestor with our pin is not on the line we
        # track, so no rebase reaches it and it is not a gap — PROVIDED we are
        # anchored, i.e. we contain some release of this project. With no anchor
        # the same observation could equally mean upstream re-rooted its history
        # and left us on the abandoned side, so it stays undecided.
        if g.get("disjoint"):
            if base_g is not None:
                g["verdict"] = SUPERSEDED
                g["basis"] = {"kind": "disjoint", "sha": base_g["sha"],
                              "tag": base_g["tags"][0]}
                g["why"] = ("shares no ancestor with the line we track — an abandoned "
                            "history, not a release any rebase could reach")
            else:
                g["verdict"] = None
                g["why"] = ("shares no ancestor with our pinned ref, and we contain no "
                            "release of this project to anchor the comparison")
            continue
        tg = _t(g["sha"])
        if not ref_t or tg is None:
            g["verdict"] = None
            g["why"] = ("where this release's line left the upstream trunk could not be "
                        "established, so whether it is ahead of us or behind us is "
                        "undecided" + (
                            "" if fp else
                            " (our own merge-base with the upstream trunk is unknown)"))
            continue
        anc = _ancestor(graph, tool, up_full, ref_t, tg, out)
        if anc is None:
            g["verdict"] = None
            g["why"] = ("the trunk order between this release and the one we build "
                        "could not be established")
        elif not anc:
            g["verdict"] = SUPERSEDED
            g["basis"] = ({"kind": "trunk-order", "sha": base_g["sha"],
                           "tag": base_g["tags"][0]} if base_g is not None else
                          {"kind": "trunk-order-without-a-base"})
            g["why"] = (f"its line left the upstream trunk at {tg[:12]}, before the line of "
                        f"{base_g['tags'][0] if base_g else 'our pinned ref'} left it at "
                        f"{ref_t[:12]} — an older series, not a release we could advance to")

    # 5b. RE-PROVE THE BUCKETS THAT TAKE A RELEASE OUT OF THE COUNT. Step 3b could
    #     not: SUPERSEDED and FOLDED do not exist until steps 4 and 5 have run. So
    #     the same treatment is applied the moment they do, from the BASIS each
    #     step recorded, by implementations that do not go through `git cherry`
    #     (`_carried_by`) or through `_ancestor`'s cached trunk points.
    #
    #     Round 3 re-proved only the buckets that assert we HAVE something. On the
    #     36 real clones, offline, that left 59 SUPERSEDED rows re-proved by
    #     nothing — including both of the live zeroes that rest entirely on them.
    #     A wrong CONTAINED overstates our health loudly; a wrong SUPERSEDED
    #     deletes a release we owe from the count and reads exactly like health.
    out["bucket_check"] = _merge_checks(
        out["bucket_check"],
        _verify_buckets(graph, groups, pin_sha, LATE_VERIFIED_BUCKETS, fp=fp))

    # 6. REPORT. `base_release` is the newest release we were MEASURED to contain
    #    — the release we actually build — not the newest one dated before our
    #    fork point, which is what named the wrong tag on three rows at once.
    for g in groups.values():
        row = {"tag": g["tags"][0], "date": g.get("date"), "sha": g["sha"][:12],
               "why": g.get("why")}
        if len(g["tags"]) > 1:
            row["also_tagged"] = g["tags"][1:]
        if g["verdict"] == CONTAINED:
            out["contained"].append(row)
        elif g["verdict"] == EQUIVALENT:
            out["equivalent"].append(row)
        elif g["verdict"] == NEW:
            out["new"].append(row)
        elif g["verdict"] == SUPERSEDED:
            out["superseded"].append(row)
        elif g["verdict"] == FOLDED:
            out["folded"].append({**row, "counted_under": g.get("folded_into")})
        else:
            out["undetermined"].append({**row, "error": g.get("why") or "undetermined",
                                        # See `_verify_buckets`: a refuted row was
                                        # MEASURED not to be contained; an unmeasured
                                        # one was not measured at all.
                                        "refuted": bool(g.get("refuted"))})
    # `base_release` — AND WHEN IT MUST NOT BE NAMED AT ALL.
    #
    # Round 4 established that a row the re-proof REFUTED must not be published as
    # the release we build, and that the honest replacement is the newest release
    # that survived the check, "not nothing" — because a refutation is a
    # measurement: the repository was asked and it says our pin does not hold that
    # release, so an older one really is the newest we hold.
    #
    # A row NOTHING COULD MEASURE gives no such licence, and stepping over it is
    # the same fall-through this round removes at `_local_containment`, one field
    # further on. Measured: with `merge-tree` never returning, the release our pin
    # genuinely builds became undetermined and `base_release` moved from `v1.0` to
    # `v0.9` — a different, older tag published as "the release we build", from a
    # sweep that had just recorded that it could not tell. The count is already
    # withheld for the same reason two lines below; this is the same claim about
    # the same fact and it is withheld with it, under the same status.
    unmeasured = [r["tag"] for r in out["undetermined"] if not r.get("refuted")]
    out["base"] = None if unmeasured else (base_g["tags"][0] if base_g is not None else None)
    if unmeasured and base_g is not None:
        out["base_withheld"] = (
            f"the newest release our pinned ref was measured to contain is "
            f"{base_g['tags'][0]}, but containment could not be measured at all for "
            f"{', '.join(unmeasured)}, so which release we build is not established")
    # WHAT THE RE-PROOF COVERED, beside what it found. `checked` alone is a count
    # of successes, and a count of successes cannot say which rows nobody looked
    # at — which is precisely how 59 SUPERSEDED rows went unexamined behind a
    # line reading "271 rows re-proved". Coverage is published per bucket so the
    # unverified part of the ledger is visible in the ledger.
    out["bucket_check"]["coverage"] = {
        "buckets_reproved": sorted(set(VERIFIED_BUCKETS) | set(LATE_VERIFIED_BUCKETS)),
        "rows": {k: len(out[k]) for k in
                 ("contained", "equivalent", "new", "superseded", "folded", "undetermined")},
        # An undetermined row is the only kind no re-proof is owed, because it
        # asserts nothing about the release: it says we could not find out.
        "rows_that_assert_nothing": len(out["undetermined"])}
    if out["undetermined"]:
        out["status"], out["behind"] = UNKNOWN, None
    else:
        out["status"], out["behind"] = MEASURED, len(out["new"])
    return out


def _ancestor(clone, tool: str, up_full: str, a: str, b: str, out: dict):
    """Is commit `a` an ancestor of commit `b`? None when it cannot be decided.

    None is a THIRD value on purpose. Every caller here has to choose what to do
    with "could not tell", and each of them must choose the answer that does not
    invent a fact: a prerelease that cannot be shown to be superseded stays
    counted, and a release whose position cannot be established is undetermined.

    AND THE THIRD VALUE ONLY WORKS IF THE FAILURES REACH IT. `rc in (0, 1)` was
    true of a `git` that never ran, because `_git` returned 1 for every
    exception — so a timeout became a definite "not an ancestor" and returned
    without ever falling through to the API. That is the reading that decides
    `SUPERSEDED` at the trunk-order site: "its line left the upstream trunk
    before ours did" is what a failed subprocess used to say, and SUPERSEDED
    REMOVES the release from the count.
    """
    if clone is not None:
        r = _git(clone, "merge-base", "--is-ancestor", a, b)
        if r.ran and r.rc in (0, 1):
            return r.ok
    if out["api_calls"] >= API_PROBE_CAP:
        return None
    out["api_calls"] += 1
    c = gh(f"repos/{up_full}/compare/{b}...{a}")
    if c.get("_err") or not isinstance(c.get("ahead_by"), int):
        return None
    return c["ahead_by"] == 0


def _carried_by(clone, tool: str, up_full: str, a: str, b: str, out: dict):
    """Does `b` already carry every commit `a` has? None when it cannot be decided.

    The SAME question `_local_containment` asks of our pinned ref, asked of one
    release about another, and answered by the same two tests in the same order:
    ancestry first because it is free, then patch-equivalence, which is the half
    the prerelease fold was missing.

    None is a third value on purpose, and every caller must pick the answer that
    invents no fact: a prerelease that cannot be SHOWN to be carried stays
    counted. Not being able to tell never removes a release from the count.
    """
    anc = _ancestor(clone, tool, up_full, a, b, out)
    if anc is True:
        return True
    if clone is not None and _patch_equivalent(clone, a, b).proved:
        return True
    return None if anc is None else False


def _merge_base(clone, tool: str, up_full: str, a: str, b: str, out: dict):
    """The merge-base commit of `a` and `b`, or None if it could not be found."""
    if a == b:
        return a
    if clone is not None:
        r = _git(clone, "merge-base", a, b)
        if r.ok and re.fullmatch(r"[0-9a-f]{40}", r.out or ""):
            return r.out
    if out["api_calls"] >= API_PROBE_CAP:
        return None
    for repo in (f"{ORG}/{tool}", up_full):
        out["api_calls"] += 1
        c = gh(f"repos/{repo}/compare/{a}...{b}")
        sha = ((c.get("merge_base_commit") or {}).get("sha")
               if isinstance(c, dict) and not c.get("_err") else None)
        if sha:
            return sha
        if out["api_calls"] >= API_PROBE_CAP:
            break
    return None


def release_gap_status(led: dict) -> str:
    """MEASURED / UNKNOWN / NOT_PROBED for one ledger row — THREE claims, not two.

    They are three different sentences and the difference is the point of this
    module:

      MEASURED   — we asked about every upstream release and this is the answer.
      UNKNOWN    — we asked and at least one release could not be decided. The
                   count does not exist; `undetermined_releases` says what stopped
                   each one.
      NOT_PROBED — the question has no subject: nothing pins this tool into the
                   image, or the upstream publishes no release or tag at all.
                   Measured on the corpus the day this was written: four upstreams
                   with zero tags and seven tools with no pin, ELEVEN rows whose
                   ledger value is null. Rendering them "0" claims we compared
                   against something. There was nothing to compare against.

    A ledger written before `behind_releases_status` existed is read at face
    value: a count is a count, and a null with undetermined rows beside it is
    UNKNOWN. A null with nothing beside it never had a subject either.
    """
    st = led.get("behind_releases_status")
    if st in (MEASURED, UNKNOWN, NOT_PROBED):
        return st
    if led.get("behind_releases") is None:
        return UNKNOWN if led.get("undetermined_releases") else NOT_PROBED
    return MEASURED


def release_gap(led: dict):
    """The ledger's release gap as an int, or None when it is not a measurement.

    THE ONE READER every consumer must go through. `led.get("behind_releases") or
    0` is the shape being removed: it maps "we could not find out" onto the same
    value as "we checked and there is nothing", and no reader downstream can tell
    those apart — which is the whole defect.

    `or 0` was ALSO still here, one level down: `return n if isinstance(n, int)
    else 0` handed a confident zero to every NOT_PROBED row, because only UNKNOWN
    was being screened out. A null is a null under both statuses; only MEASURED
    produces a number.
    """
    if release_gap_status(led) != MEASURED:
        return None
    n = led.get("behind_releases")
    return n if isinstance(n, int) else None


def release_gap_unknown(led: dict) -> bool:
    """Could containment NOT be decided for at least one upstream release?

    True is not a small number; it is the absence of one. A consumer that renders
    it as 0, or as a guess, is publishing a measurement nobody made.

    NOT the negation of "measured": a NOT_PROBED row is not unknown-in-this-sense
    — nobody failed to measure anything, there was nothing to measure — and the
    callers that escalate an unknown gap to a human must not escalate those.
    `release_gap() is None` is the test for "there is no number here".
    """
    return release_gap_status(led) == UNKNOWN


def _ls_remote_head(url: str, branch: str) -> str | None:
    """The commit `refs/heads/<branch>` points at, from ONE `git ls-remote`.

    Costs no API budget and needs no fork network. It is what makes answering
    from a local clone SAFE rather than merely cheap: a clone that has not been
    fetched would report a stale merge-base and, worse, a small `behind_commits`,
    which is the reassuring direction. Asking the remote what the branch really
    is turns "the clone might be current" into a checked fact.
    """
    try:
        r = subprocess.run(["git", "ls-remote", url, f"refs/heads/{branch}"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return parts[0]
    return None


def _local_compare(tool: str, up_full: str, up_branch: str, head: str):
    """`compare`-shaped answer for `<upstream branch>...<our head>` from the clone.

    Same keys the API compare returns — `merge_base_commit`, `ahead_by`,
    `behind_by`, `commits` — so the caller reads one shape whichever route
    answered. None means THIS CLONE CANNOT ANSWER, which is not an answer of zero.

    Refuses to answer unless it holds the CURRENT upstream branch head, resolved
    by `git ls-remote` rather than trusted from a remote-tracking ref. A stale
    clone reports fewer commits behind than there are, and every consumer reads
    small numbers as health.
    """
    clone = _clone_for(tool)
    if clone is None or not head or not up_branch:
        return None
    up_sha = _ls_remote_head(f"https://github.com/{up_full}.git", up_branch)
    if not up_sha:
        return None
    have = _peel(clone, [up_sha, head])
    if have is None or up_sha not in have or head not in have:
        return None
    up_sha, head_sha = have[up_sha], have[head]
    mbr = _git(clone, "merge-base", up_sha, head_sha)
    if not mbr.ok or not re.fullmatch(r"[0-9a-f]{40}", mbr.out or ""):
        return None
    mb = mbr.out
    lr = _git(clone, "rev-list", "--left-right", "--count", f"{up_sha}...{head_sha}")
    parts = lr.out.split()
    if not lr.ok or len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    lg = _git(clone, "log", f"--max-count={CAP}",
              "--format=%H%x1f%ad%x1f%s", "--date=short", f"{mb}..{head_sha}")
    commits = []
    if lg.ok and lg.out:
        for line in reversed(lg.out.splitlines()):
            f = line.split("\x1f")
            if len(f) == 3:
                commits.append({"sha": f[0], "html_url": f"https://github.com/{ORG}/{tool}/commit/{f[0]}",
                                "commit": {"message": f[2], "author": {"date": f[1]}}})
    shw = _git(clone, "show", "-s", "--format=%H%x1f%ad%x1f%s", "--date=short", mb)
    mf = shw.out.split("\x1f") if shw.ok else []
    mb_commit = {"sha": mb, "html_url": f"https://github.com/{up_full}/commit/{mb}",
                 "commit": {"message": mf[2] if len(mf) == 3 else "",
                            "author": {"date": mf[1] if len(mf) == 3 else ""}}}
    return {"merge_base_commit": mb_commit, "ahead_by": ahead, "behind_by": behind,
            "commits": commits}


def discover_one(fork: dict, pins: dict, image_version: str) -> dict:
    tool, up_full = fork["tool"], fork["upstream"]
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    led = {"tool": tool, "role": fork.get("role", ""),
           "upstream": up_full, "upstream_url": f"https://github.com/{up_full}",
           "fork_url": f"https://github.com/{ORG}/{tool}",
           "image_version": image_version, "generated_at": now}

    meta = gh(f"repos/{ORG}/{tool}")
    if meta.get("_err"):
        led["error"] = f"repo meta: {meta['_err']}"
        return led
    parent = meta.get("parent") or {}
    # ASK THE UPSTREAM when our repo has no parent. `vibeic/OpenROAD-flow-scripts`
    # and `vibeic/ASAP7_for_KLayout` are independent repositories rather than
    # GitHub forks (isFork=false, parent=null), so `parent.default_branch` is
    # absent and the hardcoded "main" was wrong for ORFS, whose upstream uses
    # `master`. Every compare against that branch then failed, `behind_commits`
    # never computed, and the row read CLEAN while our pin (v3.0) sat 5944
    # commits behind the upstream tag the ledger itself recorded as latest
    # (vibeic-eda#33). `up_full` was never the problem — it comes from
    # FORKS.json — only the branch did.
    up_branch = parent.get("default_branch")
    if not up_branch:
        _um = gh(f"repos/{up_full}")
        up_branch = (_um.get("default_branch")
                     if isinstance(_um, dict) and not _um.get("_err") else None)
    # Still nothing means the upstream could not be asked. "main" is a guess, and
    # a guessed branch produces a failed compare that reads as "nothing behind" —
    # so it is recorded as unknown instead, and the caller sees no comparison
    # rather than a comparison against a branch that may not exist.
    led["upstream_branch_resolved"] = bool(up_branch)
    up_branch = up_branch or "main"
    up_owner = up_full.split("/")[0]
    led.update({"forked_at": (meta.get("created_at") or "")[:10],
                "upstream_default_branch": up_branch})

    pin = pins.get(tool.lower()) or {}
    ref = pin.get("ref")
    led["pinned_ref"] = (ref or "")[:12] if ref else None
    led["pinned_ref_full"] = ref
    led["vibeic_branch"] = pin.get("branch")
    led["dockerfile_arg"] = pin.get("arg")
    # A fork the image never fetches is forked but NOT layered in. Track it honestly:
    # such a tool uses upstream directly, so there is nothing to sync into the image.
    # `integrated` = REACHES THE SHIPPED IMAGE, by either route — its own ARG pin, or
    # vendored inside one (vibeic/vibeic-eda#8). The category is unchanged and still
    # holds forks that are genuinely absent; what changed is who belongs to it.
    led["integrated"] = bool(ref)
    if pin.get("vendored_in"):
        # HOW it is pinned, kept on the row because it is a different operational fact:
        # this ref moves when the HOST's gitlink moves, so changing it means rebuilding
        # the host. `pinned_via` is the string the monitor page renders.
        led["vendored_in"] = pin["vendored_in"]
        led["vendored_path"] = pin["vendored_path"]
        led["vendored_host_ref"] = pin.get("host_ref")
        led["pinned_via"] = (f"{pin.get('arg') or '?'} → {ORG}/{pin['vendored_in']} "
                             f"{pin['vendored_path']}")

    # our carried patches + fork point: where our pinned ref left the upstream trunk.
    #
    # ROUTED ON WHAT THE REPOSITORY IS, NOT ON AN ERROR IT RETURNS. The query this
    # used to lead with — `repos/vibeic/<tool>/compare/<up_owner>:<branch>...<head>`
    # — is a CROSS-REPO compare, which GitHub resolves only through a shared fork
    # network. Several of our repositories are mirrors rather than GitHub forks
    # (`fork: false`, `parent: null`), and for those it does not fail
    # intermittently: it 404s 100% of the time, permanently, and the reversed
    # query 404s for the same reason (measured on `repos/AUCOHL/Fault/compare/
    # main...<our pin>` → HTTP 404). `fork`/`parent` is a fact the meta call
    # already fetched, so the form that CAN answer is chosen up front rather than
    # discovered by spending a request on one that cannot.
    #
    # THE LOCAL CLONE ANSWERS FIRST, for the same reason it does on the release
    # path: it holds both sides in one object store, so the merge-base is a local
    # graph question with no API budget and no fork network in it at all. A mirror
    # with a clone was the row that made this necessary — `fork_point` stayed None,
    # so every release became UNDETERMINED and the tool was permanently
    # `behind_releases: null` over three junk test tags, while
    # `git merge-base upstream/<branch> <pin>` in the clone answered in
    # milliseconds. Fixing the release path and leaving this one on the broken
    # form is half a fix.
    #
    # UNDETERMINED ONLY WHEN NEITHER CAN. `fork_point_status` records which route
    # answered, and `compare_error` carries the literal text when none did — the
    # row must never read as "no divergence" because the question failed.
    head = ref or meta.get("default_branch") or up_branch
    # A repo that STATES it is not a fork cannot answer the cross-repo form. An
    # older ledger's meta that states nothing is not evidence either way, so the
    # query is still attempted — routing on a fact we have, never on a guess.
    can_cross_repo = bool(meta.get("parent")) or meta.get("fork") is not False
    cmp: dict = {"_err": "not attempted"}
    source = None

    # The clone answers about our PIN. An unpinned tool has no ref to ask about,
    # and resolving its fork's branch name inside a clone would answer about a
    # different commit than the one the API would name.
    _loc = _local_compare(tool, up_full, up_branch, ref) if ref else None
    if _loc is not None:
        cmp, source = _loc, "local-clone"
    if cmp.get("_err") and can_cross_repo:
        cmp = gh(f"repos/{ORG}/{tool}/compare/{up_owner}:{up_branch}...{head}")
        if not cmp.get("_err"):
            source = "fork-network-compare"
    if cmp.get("_err") and ref:
        # The reversed query answers the same question from the other side:
        # upstream's `<branch>...<our ref>` reports how far our ref trails.
        # `ahead_by`/`behind_by` swap meaning with the direction, so they are read
        # back the other way round. It is NOT gated on the fork network: it
        # resolves whenever the upstream repo can name our ref itself, which is
        # the case for every tool pinned to an upstream TAG.
        _rev = gh(f"repos/{up_full}/compare/{up_branch}...{ref}")
        if not _rev.get("_err"):
            cmp = {"merge_base_commit": _rev.get("merge_base_commit"),
                   "ahead_by": _rev.get("ahead_by", 0),
                   "behind_by": _rev.get("behind_by", 0),
                   "commits": _rev.get("commits") or []}
            led["compare_direction"], source = "from-upstream", "upstream-compare"
    if cmp.get("_err") and not can_cross_repo:
        cmp = {"_err": (f"{cmp['_err']}; {ORG}/{tool} is a mirror rather than a GitHub "
                        f"fork (fork=false, parent=null), so no cross-repo compare "
                        f"against {up_full} can resolve, and no local clone could answer")}
    pin_date = None
    if not cmp.get("_err"):
        mb = cmp.get("merge_base_commit") or {}
        led["fork_point"] = _commit_brief(mb) if mb else None
        led["ahead"] = cmp.get("ahead_by", 0)                 # our patches on the pinned branch
        led["behind_commits"] = cmp.get("behind_by", 0)       # informational (commit granularity)
        led["carried_patches"] = [_commit_brief(c) for c in (cmp.get("commits") or [])][:CAP]
        led["fork_point_status"] = source
        # Classify releases by the FORK POINT (merge-base) date — the point where our
        # branch diverges from upstream = the release our patches are based on. Using a
        # patch's own author date is wrong: rebasing onto a new release preserves the
        # patch author dates, so the fork-point is the only reliable "we're based on X".
        pin_date = (led.get("fork_point") or {}).get("date")
    else:
        led["compare_error"] = cmp["_err"]
        led["fork_point_status"] = "undetermined"

    # RELEASE tracking, by CONTAINMENT — see the block above `classify_releases`.
    # `pin_date` (the fork-point date) stays in the ledger as display metadata; no
    # arithmetic that produces `behind_releases` reads it any more.
    rels = _releases(up_full)
    if rels is None:
        # #49: neither the release endpoint nor the tag feed could be asked. A
        # classification run over an empty list would answer "behind 0, MEASURED"
        # — the exact sentence this file spent five rounds removing from the
        # containment path. The vocabulary for this already exists; use it.
        led["upstream_releases"] = []
        led["upstream_latest_release"] = None
        led["release_source_error"] = ("neither the releases endpoint nor the "
                                       "tag feed could be read")
        cl = {"new": [], "contained": [], "equivalent": [], "superseded": [],
              "folded": [], "undetermined": [], "behind": None,
              "status": "UNKNOWN", "base": None, "api_calls": 0, "clone": None}
    else:
        led["upstream_releases"] = rels[:15]
        led["upstream_latest_release"] = rels[0]["tag"] if rels else None
        cl = classify_releases(tool, up_full, rels, ref,
                               fork_point=(led.get("fork_point") or {}).get("sha"))
    led["new_releases"] = cl["new"]
    led["contained_releases"] = cl["contained"][:15]
    # NAMED FOR THE CLAIM IT MAKES. Our pinned ref carries every commit these
    # releases have, under different shas — and has since moved past them, so
    # merging one is not a no-op and `contained` would overstate it. They are not
    # counted, and they remain eligible to be `base_release`: yices2's
    # `yices-2.7.0` is exactly this shape and is the release yices2 builds.
    led["patch_equivalent_releases"] = cl["equivalent"][:15]
    led["superseded_releases"] = cl["superseded"][:15]
    # NAMED FOR WHAT IS IN IT. These carry commits our pinned ref does NOT have —
    # they are counted, once, under the release that carries their work. Filing
    # them beside the releases we already build made the ledger assert something
    # measurably false: two of them compared 225 and 15 commits ahead of our pin,
    # with 300+ changed files, under the heading "contained".
    led["folded_releases"] = cl["folded"][:15]
    led["undetermined_releases"] = cl["undetermined"]
    led["behind_releases"] = cl["behind"]
    led["behind_releases_status"] = cl["status"]
    led["release_containment"] = {"clone": cl["clone"], "api_calls": cl["api_calls"],
                                  "candidates": len(rels), "fork_point_date": pin_date,
                                  # WHAT WAS RE-PROVED, and how much of it. `checked`
                                  # is here so a clean result over zero rows reads as
                                  # zero rows rather than as a pass.
                                  "bucket_check": cl["bucket_check"],
                                  # WHY THERE IS NO `base_release`, when there is a
                                  # release we were measured to contain but a row
                                  # nobody could measure sits beside it. A null with
                                  # no sentence next to it is the value this module
                                  # exists to stop publishing.
                                  "base_withheld": cl.get("base_withheld")}
    led["base_release"] = cl["base"]

    led.setdefault("last_sync", None)
    return led


def main():
    # vibeic/vibeic-eda#12. Checked BEFORE the first upstream call: the ledger directory is
    # shared production state (#10 proved a stale ledger publishes a frozen row that reads
    # exactly like a live one), so a process that may not write it must not spend a
    # fleet-wide discovery finding that out.
    gk_state.require_writable(LEDGER, "the fork ledgers")
    LEDGER.mkdir(parents=True, exist_ok=True)
    # The pins moved out of the single Dockerfile when each tool got its own
    # (vibeic-eda#14). Reading only the root file would find zero pins and fall
    # through to the warning below — a silent downgrade to default-branch tracking
    # for every tool, indistinguishable from "these forks have no pin".
    parts = [_gh_file(EDA_REPO, "Dockerfile") or ""]
    listing = gh(f"repos/{EDA_REPO}/contents/tools")
    tool_dirs = [e["name"] for e in listing
                 if isinstance(e, dict) and e.get("type") == "dir"] \
                if isinstance(listing, list) else []
    for _d in sorted(tool_dirs):
        parts.append(_gh_file(EDA_REPO, f"tools/{_d}/Dockerfile") or "")
    df = "\n".join(parts)
    if tool_dirs:
        print(f"  read pins from Dockerfile + {len(tool_dirs)} tools/*/Dockerfile")
    pins = parse_dockerfile_pins(df)
    image_version = (_gh_file(EDA_REPO, "VERSION") or "").strip() or "unknown"
    if not pins:
        # Before the per-tool split an empty parse could mean "no ARG pins yet".
        # Now the layout guarantees eight, so empty means the parser no longer
        # matches the files — and default-branch tracking would publish a page
        # that looks identical to a healthy one while measuring something else.
        print("  WARNING: could not parse ANY pin from Dockerfile or tools/*/Dockerfile.")
        print("           That is a parser/layout mismatch, not a repo without pins.")
        print("           Falling back to default-branch tracking; rows derived this")
        print("           way state a branch head, NOT what the image ships.")
    direct = set(pins)
    pins = expand_vendored_pins(pins)
    for name in sorted(set(pins) - direct):
        p = pins[name]
        print(f"  {p['repo']:16} vendored in {ORG}/{p['vendored_in']} at {p['vendored_path']} "
              f"(pinned via {p.get('arg')}) — in the image, no ARG of its own")

    index = []
    # Tools whose own classification was refuted by the re-proof in step 3b. Not a
    # warning: the sweep exits non-zero on it, because a bucket that asserts
    # something false is the defect this module was rewritten to remove and it must
    # not be able to reappear quietly.
    refuted: dict[str, list] = {}
    unmeasured: dict[str, list] = {}
    for fork in FORKS:
        prev = LEDGER / f"{fork['tool']}.json"
        sync_log, last_sync = [], None
        if prev.is_file():
            try:
                old = json.loads(prev.read_text())
                sync_log, last_sync = old.get("sync_log", []), old.get("last_sync")
            except json.JSONDecodeError:
                pass
        led = discover_one(fork, pins, image_version)
        led["sync_log"], led["last_sync"] = sync_log, last_sync
        led[gk_state.PROVENANCE_KEY] = gk_state.provenance()
        prev.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
        # `behind_releases_status` travels WITH the count everywhere the count
        # travels. The index is what `build_page` and every quick reader load
        # first, and a null in it with no status beside it is exactly the value
        # nobody can tell from a measurement.
        index.append({k: led.get(k) for k in (
            "tool", "role", "upstream", "forked_at", "pinned_ref", "vibeic_branch",
            "ahead", "base_release", "upstream_latest_release", "behind_releases",
            "behind_releases_status", "image_version", "last_sync")})
        _bc = (led.get("release_containment") or {}).get("bucket_check") or {}
        if _bc.get("violations"):
            refuted[fork["tool"]] = _bc["violations"]
        # …AND THE ROWS NOTHING COULD MEASURE, which are not the same thing and
        # must not be reported as the same thing. See the exit status below.
        _un = [r for r in (led.get("undetermined_releases") or []) if not r.get("refuted")]
        if _un:
            unmeasured[fork["tool"]] = _un
        _st = release_gap_status(led)
        _n = ("unknown" if _st == UNKNOWN
              else ("not-probed" if _st == NOT_PROBED else release_gap(led)))
        tag = led.get("error") or (f"pin={led.get('pinned_ref')} patches={led.get('ahead','?')} "
                                   f"base={led.get('base_release')} latest={led.get('upstream_latest_release')} "
                                   f"new_releases={_n}")
        print(f"  {fork['tool']:16} {tag}")
    (LEDGER / "index.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
         "image_version": image_version,
         gk_state.PROVENANCE_KEY: gk_state.provenance(), "forks": index},
        indent=2, ensure_ascii=False) + "\n")
    _checks = [((json.loads((LEDGER / f"{f['tool']}.json").read_text())
                 .get("release_containment") or {}).get("bucket_check") or {})
               for f in FORKS if (LEDGER / f"{f['tool']}.json").is_file()]
    _checked = sum(c.get("checked", 0) for c in _checks)
    _unver = sum(len(c.get("unverifiable") or []) for c in _checks)
    _by: dict[str, int] = {}
    for c in _checks:
        for k, v in (c.get("by_bucket") or {}).items():
            _by[k] = _by.get(k, 0) + v
    # SAY WHICH BUCKETS, not just how many rows. A total is the shape that let 59
    # unexamined SUPERSEDED rows sit behind a reassuring number.
    print(f"wrote {len(index)} ledgers · image {image_version} → {LEDGER}")
    print(f"  bucket re-proof: {_checked} row(s) re-proved from the clones "
          f"({', '.join(f'{k}={v}' for k, v in sorted(_by.items())) or 'none'}), "
          f"{_unver} unverifiable, {len(refuted)} tool(s) refuted")
    if refuted:
        print("  BUCKET INVARIANT VIOLATED — these rows made a claim their own "
              "repository refutes. They were held UNDETERMINED, not published:")
        for tool, vs in refuted.items():
            for v in vs:
                print(f"    {tool:16} {v['tag']:24} [{v['claim']}] {v['reason']}")
    if unmeasured:
        print("  NOT MEASURED — containment could not be decided for these releases, so "
              "the count and `base_release` are withheld for their tools:")
        for tool, rows in unmeasured.items():
            for r in rows:
                print(f"    {tool:16} {r['tag']:24} {(r.get('error') or '')[:110]}")
    # THREE OUTCOMES, THREE EXIT STATUSES, and the third one is what this round
    # adds. See `EXIT_*`.
    if refuted:
        return EXIT_REFUTED
    return EXIT_NOT_MEASURED if unmeasured else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
