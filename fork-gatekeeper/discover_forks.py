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
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent          # version-controlled source
STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
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


def _releases(up_full: str) -> list[dict]:
    """Upstream releases newest-first: [{tag, date}]. Falls back to tags (no dates)."""
    rel = gh(f"repos/{up_full}/releases?per_page=30")
    out = []
    if isinstance(rel, list) and rel:
        for r in rel:
            out.append({"tag": r.get("tag_name"), "date": (r.get("published_at") or "")[:10]})
        return out
    tags = gh(f"repos/{up_full}/tags?per_page=30")
    if isinstance(tags, list):
        for t in tags:
            out.append({"tag": t.get("name"), "date": None})
    return out


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
    LEDGER.mkdir(parents=True, exist_ok=True)
    df = _gh_file(EDA_REPO, "Dockerfile") or ""
    pins = parse_dockerfile_pins(df)
    image_version = (_gh_file(EDA_REPO, "VERSION") or "").strip() or "unknown"
    if not pins:
        print("  WARNING: could not parse Dockerfile pins (falling back to default-branch tracking)")
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
         "image_version": image_version, "forks": index}, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(index)} ledgers · image {image_version} → {LEDGER}")


if __name__ == "__main__":
    main()
