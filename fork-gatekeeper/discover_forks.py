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
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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
        tail = text[m.end(): m.end() + 400]
        am = re.search(r"\$\{(\w+_REF)\}", tail)
        if am and am.group(1) in args:
            arg = am.group(1)
            step = next((t for s, e, t in instrs if s <= m.start() < e), "")
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


def _tags_by_date(up_full: str, limit: int = 30) -> list[dict]:
    """Tags newest-first WITH dates, in one call. [] if it could not be asked.

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
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    try:
        nodes = (json.loads(r.stdout).get("data") or {}).get(
            "repository", {}).get("refs", {}).get("nodes") or []
    except (json.JSONDecodeError, AttributeError):
        return []
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


def _releases(up_full: str) -> list[dict]:
    """Upstream versions newest-first: [{tag, date}] — releases AND tags, merged.

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
    rel = gh(f"repos/{up_full}/releases?per_page=30")
    if isinstance(rel, list):
        for r in rel:
            tag = r.get("tag_name")
            if tag:
                merged[tag] = _iso_date(r.get("published_at"))

    for t in _tags_by_date(up_full):
        merged.setdefault(t["tag"], t["date"])

    if not merged:
        # Neither source answered. NOT "this project has no versions" — a caller
        # that reads an empty list here concludes we are current, which is the
        # failure this function is being fixed for. Left empty deliberately and
        # visibly: the ledger records upstream_latest_release=None, which is a
        # missing value rather than a reassuring one.
        return []

    # Undated entries sort last rather than first: an unknown date must never
    # win the "latest" slot that drives the ancestry compare.
    return [{"tag": k, "date": (v or None)}
            for k, v in sorted(merged.items(),
                               key=lambda kv: (kv[1] or "", kv[0]), reverse=True)]


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
    up_branch = parent.get("default_branch") or "main"
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

    # our carried patches + fork point: compare upstream default ... our pinned ref
    head = ref or meta.get("default_branch") or up_branch
    cmp = gh(f"repos/{ORG}/{tool}/compare/{up_owner}:{up_branch}...{head}")
    pin_date = None
    if not cmp.get("_err"):
        mb = cmp.get("merge_base_commit") or {}
        led["fork_point"] = _commit_brief(mb) if mb else None
        led["ahead"] = cmp.get("ahead_by", 0)                 # our patches on the pinned branch
        led["behind_commits"] = cmp.get("behind_by", 0)       # informational (commit granularity)
        led["carried_patches"] = [_commit_brief(c) for c in (cmp.get("commits") or [])][:CAP]
        # Classify releases by the FORK POINT (merge-base) date — the point where our
        # branch diverges from upstream = the release our patches are based on. Using a
        # patch's own author date is wrong: rebasing onto a new release preserves the
        # patch author dates, so the fork-point is the only reliable "we're based on X".
        pin_date = (led.get("fork_point") or {}).get("date")
    else:
        led["compare_error"] = cmp["_err"]

    # RELEASE tracking. Accurate "are we on the latest release" via ANCESTRY (one
    # compare: is the latest release tag contained in our pinned ref?) — date-based
    # classification is fragile for tools that release daily (magic) or whose tags
    # aren't on the default branch. Fall back to dates only when not current.
    rels = _releases(up_full)
    led["upstream_releases"] = rels[:15]
    led["upstream_latest_release"] = rels[0]["tag"] if rels else None
    new, base = [], None
    current = False
    if rels and ref:
        latest_tag = rels[0]["tag"]
        c = gh(f"repos/{ORG}/{tool}/compare/{up_owner}:{latest_tag}...{ref}")
        # behind_by == 0 → the latest release has no commit our pin lacks → we're current
        if not c.get("_err") and c.get("behind_by", 1) == 0:
            base, current = latest_tag, True
    if not current:
        new = [r for r in rels if r.get("date") and pin_date and r["date"] > pin_date]
        b = next((r for r in rels if r.get("date") and pin_date and r["date"] <= pin_date), None)
        base = b["tag"] if b else None
    led["new_releases"] = new
    led["behind_releases"] = len(new)
    led["base_release"] = base

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
        index.append({k: led.get(k) for k in (
            "tool", "role", "upstream", "forked_at", "pinned_ref", "vibeic_branch",
            "ahead", "base_release", "upstream_latest_release", "behind_releases",
            "image_version", "last_sync")})
        tag = led.get("error") or (f"pin={led.get('pinned_ref')} patches={led.get('ahead','?')} "
                                   f"base={led.get('base_release')} latest={led.get('upstream_latest_release')} "
                                   f"new_releases={led.get('behind_releases','?')}")
        print(f"  {fork['tool']:16} {tag}")
    (LEDGER / "index.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
         "image_version": image_version,
         gk_state.PROVENANCE_KEY: gk_state.provenance(), "forks": index},
        indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(index)} ledgers · image {image_version} → {LEDGER}")


if __name__ == "__main__":
    main()
