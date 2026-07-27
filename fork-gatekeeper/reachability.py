#!/usr/bin/env python3
"""reachability.py — does an upstream commit touch code WE CAN ACTUALLY REACH?

The doctrine printed under every assessment says: *understand every commit, CONFIRM
EACH BUGFIX REPRODUCES IN OUR VERSION, adopt selectively.* Nothing implemented the
middle clause. `relevant` was the language model's opinion, formed from a commit title
and one body line; no program ever checked whether the code a commit touches is code we
run (vibeic/vibeic-eda#5).

Caught live on magic 8.3.674 → 8.3.678: `3f1747b1fb91` was the range's ONE clearly-safe
row, so the armed tick would auto-propose it. Its stated reason — "critical for automated
batch DRC/extraction runs" — is not true of our fork. The patch guards `CmdCrosshair()`
and `DBWSetCrosshair()`, which are reachable only from the `crosshair` command, and we
never issue `crosshair`. The VERDICT was defensible (a NULL guard in a tool we run
headless is harmless); the EVIDENCE attached to it was not, and that evidence is what
travels into an auto-opened merge PR for a human to rely on.

WHAT THIS COMPUTES (no model, no prompt, three deterministic reads):

  1. the tool's OWN command registry, from its source — for magic, every
     `WindAddCommand(client, "name  help…", CmdFunc, …)` gives `CmdFunc -> {"name"}`;
  2. the commands that can reach the commit's touched symbols, by walking CALLERS
     upward from the functions the patch changed until each branch lands on a
     registered handler;
  3. the commands WE ISSUE, by scanning our emitter trees for command lines whose
     first token is one of the tool's own command names.

    reachable  — some command that reaches the touched code is one we issue.
    unreachable— the closure is COMPLETE, non-empty, and disjoint from our surface.
    unknown    — anything else.

"I could not determine the surface" is NOT "unreachable". A tool with no registry
extractor (klayout, netgen, yosys, … all register commands differently), a commit
absent from the local clone, a non-source change, a closure that hit its bound: every
one of those returns UNKNOWN and leaves the model's verdict standing. Silently
demoting every candidate would be the same class of error as the one this fixes —
publishing an absence of analysis as a finding.

The asymmetry is deliberate. Finding a surface command is DECISIVE even from a
truncated closure (searching further can only ADD commands, never remove one), while
concluding "unreachable" requires the closure to have finished. Over-inclusion in the
surface makes this check quiet; under-inclusion makes it demote a candidate to human
review WITH ITS EVIDENCE PRINTED, which a reviewer can overrule in one glance. Neither
error can auto-adopt anything.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

FORKS_DIR = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")
VIBEIC_REPO = Path(os.environ.get("GK_VIBEIC_REPO") or "/home/reyerchu/vibe-ic")

# Where our issued commands are written. The emitters build tool scripts as string
# literals, so this is where "what we actually run" is readable.
_DEFAULT_SURFACE_DIRS = ("vibe-ic-marketplace/plugins/vibe-ic", "mcp-eda-server", "tools")
SURFACE_EXT = {".py", ".sh", ".tcl", ".j2", ".md"}
SURFACE_MAX_BYTES = 400_000

# How far the caller walk may go before it gives up and says UNKNOWN.
MAX_DEPTH = int(os.environ.get("GK_REACH_MAX_DEPTH", "3"))
MAX_NODES = int(os.environ.get("GK_REACH_MAX_NODES", "200"))
MAX_SECONDS = float(os.environ.get("GK_REACH_MAX_SECONDS", "90"))

SOURCE_EXT = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")
_CALLER_GLOBS = ("*.c", "*.cc", "*.cpp", "*.cxx")

# A command REGISTRAR idiom, per tool family. Adding a tool means teaching this its
# registration call — never hand-listing its commands, which would rot the moment the
# tool grew one. A tool with no idiom here yields an empty registry, i.e. UNKNOWN.
_REGISTRARS = {
    # magic: WindAddCommand(clientID, "name  help text…", CmdFunc, FALSE);
    "magic": re.compile(r"WindAddCommand\s*\(\s*[A-Za-z_]\w*\s*,\s*"
                        r'((?:"(?:\\.|[^"\\])*"\s*)+),\s*([A-Za-z_]\w*)', re.S),
}
_FIRST_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"', re.S)
_CALLISH = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_C_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "defined", "else",
               "do", "case", "goto", "static", "void", "int", "char"}
_QUOTED = re.compile(r"""["']([^"'\n]{1,160})["']""")


def _git(clone: Path, *args: str, timeout: int = 60):
    """git in `clone`, or None on ANY failure. Never raises — an undecidable
    reachability question must degrade to UNKNOWN, never break the assessment."""
    try:
        return subprocess.run(["git", "-C", str(clone), *args],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None


# ── 1. the tool's own command registry ────────────────────────────────────────
def command_registry(tool: str, clone: Path) -> tuple[dict[str, set[str]], set[str]]:
    """{handler_symbol: {command names}}, plus the files that DO the registering.

    The registration file is the dispatch TABLE, not a caller: every handler's name
    appears in it, so treating it as a call site risks making every command reachable
    from every other one. It is excluded from the caller walk for that reason —
    honestly labelled as a GUARD, not a fix for an observed failure: measured on the
    real magic clone (2026-07-28) removing the exclusion changes no verdict, because
    the walk only greps NON-handler symbols and `_enclosing` only accepts a column-0
    definition, which magic's table never presents. It is kept because the invariant
    ("a dispatch table is not a call site") holds for tools that format their table
    differently, and it costs one set membership test.
    """
    pat = _REGISTRARS.get(tool)
    if pat is None or not (clone / ".git").is_dir():
        return {}, set()
    marker = pat.pattern.split(r"\s*\(")[0].lstrip("\\b")
    r = _git(clone, "grep", "-l", marker, "--", "*.c", "*.cc", "*.cpp")
    if r is None or r.returncode != 0:
        return {}, set()
    out: dict[str, set[str]] = {}
    regfiles: set[str] = set()
    for f in r.stdout.split():
        try:
            txt = (clone / f).read_text(errors="replace")
        except OSError:
            continue
        regfiles.add(f)
        for m in pat.finditer(txt):
            lit = _FIRST_LITERAL.search(m.group(1))
            words = lit.group(1).split() if lit else []
            if words:
                out.setdefault(m.group(2), set()).add(words[0])
    return out, regfiles


# ── 2. what the commit touched, and which commands reach it ──────────────────
def touched(clone: Path, sha: str) -> tuple[set[str], set[str]] | None:
    """(files, changed function symbols) for `sha`, or None if we cannot read it.

    The symbols come from git's own hunk headers (`@@ … @@ CmdCrosshair(`), which
    name the function each hunk sits in — the functions the patch CHANGED.
    """
    if not sha or not (clone / ".git").is_dir():
        return None
    if (_git(clone, "cat-file", "-e", f"{sha}^{{commit}}") or
            subprocess.CompletedProcess([], 1)).returncode != 0:
        return None
    r = _git(clone, "show", "--unified=0", "--format=", sha, timeout=120)
    if r is None or r.returncode != 0:
        return None
    files, syms = set(), set()
    for ln in r.stdout.splitlines():
        if ln.startswith("+++ b/"):
            files.add(ln[6:].strip())
        elif ln.startswith("@@"):
            m = _CALLISH.search(ln.split("@@")[-1].strip())
            if m and m.group(1) not in _C_KEYWORDS:
                syms.add(m.group(1))
    return files, syms


def _enclosing(clone: Path, path: str, lineno: int) -> str | None:
    """The function a given line sits in: the nearest definition ABOVE it that starts
    at column 0. Comment and continuation lines are skipped, so a doc block naming a
    symbol is not mistaken for a call to it."""
    try:
        lines = (clone / path).read_text(errors="replace").splitlines()
    except OSError:
        return None
    for i in range(min(lineno, len(lines)) - 1, -1, -1):
        s = lines[i]
        if not s or s[0] in "#/ \t*}":
            continue
        m = _CALLISH.search(s)
        if m and m.group(1) not in _C_KEYWORDS:
            return m.group(1)
    return None


def reachable_commands(clone: Path, symbols: set[str], reg: dict[str, set[str]],
                       regfiles: set[str], deadline: float | None = None) -> tuple[set[str], bool]:
    """(commands that can reach `symbols`, closure_complete).

    Walks CALLERS upward. A registered handler is a root — nothing above it matters,
    so the walk stops there. `closure_complete` is False the moment the walk is cut
    short by depth, node count or the deadline; a truncated closure may be MISSING
    commands, so it can support "reachable" but never "unreachable".
    """
    seen = set(symbols)
    cmds: set[str] = set()
    frontier: set[str] = set()
    for s in symbols:
        (cmds.update(reg[s]) if s in reg else frontier.add(s))
    depth = 0
    while frontier:
        if depth >= MAX_DEPTH or len(seen) >= MAX_NODES:
            return cmds, False
        nxt: set[str] = set()
        for sym in sorted(frontier):
            if deadline is not None and time.monotonic() > deadline:
                return cmds, False
            r = _git(clone, "grep", "-n", "-w", sym, "--", *_CALLER_GLOBS)
            if r is None:
                return cmds, False
            for ln in r.stdout.splitlines()[:400]:
                parts = ln.split(":", 2)
                if len(parts) < 3 or parts[0] in regfiles:
                    continue
                body = parts[2].lstrip()
                # a mention in a comment or a forward declaration is not a call site
                if body.startswith(("extern", "*", "/*", "//", "*/")):
                    continue
                try:
                    enc = _enclosing(clone, parts[0], int(parts[1]))
                except ValueError:
                    continue
                if enc and enc not in seen:
                    nxt.add(enc)
        seen |= nxt
        frontier = set()
        for sym in nxt:
            (cmds.update(reg[sym]) if sym in reg else frontier.add(sym))
        depth += 1
    return cmds, True


# ── 3. the commands WE issue ─────────────────────────────────────────────────
def _surface_roots() -> list[Path]:
    env = os.environ.get("GK_SURFACE_DIRS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    return [VIBEIC_REPO / d for d in _DEFAULT_SURFACE_DIRS]


def _looks_like_a_command_line(s: str) -> bool:
    """Is this literal plausibly a tool command line rather than English prose?

    Cheap and deliberately biased toward INCLUSION: a false positive only makes this
    check quieter, while a false negative demotes a candidate to human review. Prose
    is filtered on the things command lines do not have — sentence punctuation,
    capitalised words, one lonely word, paragraph length.
    """
    toks = s.split()
    if len(toks) < 2 or len(s) > 120:
        return False
    if any(ch in s for ch in ",;?!\u2014\u2026"):
        return False
    if s.rstrip().endswith("."):
        return False
    return not any(t[:1].isupper() for t in toks)


_SURFACE_MEMO: dict = {}


def command_surface(tool: str, known: set[str],
                    roots: list[Path] | None = None) -> tuple[dict[str, int] | None, str]:
    """({command we issue: how many command lines said so}, why-if-unknown).

    `known` is the tool's OWN command vocabulary (from its registry), so this never
    hand-maintains a command list — it only asks which of the tool's commands appear
    as the leading token of a command line in our trees. None means we could not read
    the emitters at all, which is UNKNOWN, not "we issue nothing".

    The counts exist so a report can show the surface EVIDENCE-FIRST. The prose filter
    is biased toward inclusion, so a handful of one-hit English words ride along; a
    reviewer reading "our surface is `def`, `defaults`, `element`…" would rightly stop
    trusting the claim, while "`extract` (16), `ext2spice` (22), `gds` (11)…" is the
    same set ordered so the real thing is what they see.
    """
    if not known:
        return None, "the tool exposes no command registry we can read"
    roots = roots if roots is not None else _surface_roots()
    # Memoised per process: an assessment runs this once per adopt-candidate and the
    # emitter trees do not move mid-tick.
    memo = (tool, tuple(str(r) for r in roots), tuple(sorted(known)))
    if memo in _SURFACE_MEMO:
        return _SURFACE_MEMO[memo]
    present = [r for r in roots if r.is_dir()]
    if not present:
        return _SURFACE_MEMO.setdefault(
            memo, (None, f"none of the emitter trees exist here "
                         f"({', '.join(str(r) for r in roots) or 'no roots configured'})"))
    found: dict[str, int] = {}
    for root in present:
        for p in root.rglob("*"):
            try:
                if not p.is_file() or p.suffix not in SURFACE_EXT:
                    continue
                if p.stat().st_size > SURFACE_MAX_BYTES:
                    continue
                txt = p.read_text(errors="replace")
            except OSError:
                continue
            if tool not in txt:                      # this file never mentions the tool
                continue
            for m in _QUOTED.finditer(txt):
                s = m.group(1).strip()
                if s and s.split()[0] in known and _looks_like_a_command_line(s):
                    found[s.split()[0]] = found.get(s.split()[0], 0) + 1
    if not found:
        return _SURFACE_MEMO.setdefault(
            memo, (None, "no command line for this tool was found in the emitter trees"))
    return _SURFACE_MEMO.setdefault(memo, (found, ""))


# ── the check ────────────────────────────────────────────────────────────────
def check(tool: str, sha_full: str, clone: Path | None = None,
          roots: list[Path] | None = None) -> dict:
    """Is the code this commit touches reachable from anything we run? Never raises.

    Returns {verdict, commands, surface, closure_complete, detail}. `detail` is written
    to be pasted into a review table verbatim: it must let a reader check the claim
    rather than take it, because this contradicts a model verdict.
    """
    if os.environ.get("GK_REACHABILITY", "1") not in ("1", "true", "yes"):
        return _u(tool, "the reachability check is switched off (GK_REACHABILITY=0)")
    clone = clone if clone is not None else FORKS_DIR / tool
    try:
        reg, regfiles = command_registry(tool, clone)
        if not reg:
            return _u(tool, f"no command registry could be read for {tool} — its command "
                            f"surface is not something this check knows how to derive")
        vocabulary: set[str] = set()
        for cs in reg.values():
            vocabulary |= cs
        counts, why = command_surface(tool, vocabulary, roots)
        if counts is None:
            return _u(tool, f"our issued-command surface could not be determined: {why}")
        surface = set(counts)
        got = touched(clone, sha_full)
        if got is None:
            return _u(tool, "the commit is not readable in the local clone", surface=surface)
        files, syms = got
        if not syms:
            return _u(tool, f"the patch changes no function this check can name "
                            f"({len(files)} file(s) touched) — nothing to trace",
                      surface=surface)
        cmds, complete = reachable_commands(clone, syms, reg, regfiles,
                                            deadline=time.monotonic() + MAX_SECONDS)
        hit = sorted(cmds & surface)
        if hit:
            # DECISIVE even from a truncated closure: more searching only adds commands.
            return {"verdict": REACHABLE, "commands": sorted(cmds), "surface": sorted(surface),
                    "closure_complete": complete, "symbols": sorted(syms),
                    "detail": (f"reached from {', '.join(hit)}, which our emitters issue")}
        if cmds and complete:
            return {"verdict": UNREACHABLE, "commands": sorted(cmds),
                    "surface": sorted(surface), "closure_complete": True,
                    "symbols": sorted(syms),
                    "detail": (f"the changed symbol(s) {', '.join(sorted(syms))} are reachable "
                               f"only from `{'`, `'.join(sorted(cmds))}`, and we never issue "
                               f"{'that' if len(cmds) == 1 else 'any of those'} — our "
                               f"{tool} surface is {_sample(counts)}")}
        return _u(tool, ("the caller walk hit its bound before resolving a command"
                         if not complete else
                         "the changed symbols reach no registered command"),
                  surface=surface, commands=sorted(cmds), symbols=sorted(syms),
                  complete=complete)
    except Exception as e:  # noqa: BLE001 — an undecidable question is UNKNOWN, not a crash
        return _u(tool, f"the reachability check errored ({e.__class__.__name__})")


def _sample(counts: dict[str, int], n: int = 8) -> str:
    """A bounded, EVIDENCE-ORDERED rendering of the surface — the full set lands in the
    JSON; a review row gets the commands we most demonstrably issue, not the alphabet."""
    ranked = sorted(counts, key=lambda c: (-counts[c], c))
    head = "`" + "`, `".join(ranked[:n]) + "`"
    return head if len(ranked) <= n else f"{head} (+{len(ranked) - n} more)"


def _u(tool: str, why: str, surface=None, commands=None, symbols=None,
       complete: bool | None = None) -> dict:
    """UNKNOWN. Says what it could not determine — and never demotes anything."""
    return {"verdict": UNKNOWN, "commands": commands or [],
            "surface": sorted(surface) if surface else None,
            "closure_complete": complete, "symbols": symbols or [],
            "detail": f"NOT DETERMINED — {why}"}


if __name__ == "__main__":
    import json
    import sys
    tool = sys.argv[1] if len(sys.argv) > 1 else "magic"
    for sha in sys.argv[2:] or ["3f1747b1fb915358414da70659b82cecb0314be2"]:
        print(sha, json.dumps(check(tool, sha), indent=2, ensure_ascii=False))
