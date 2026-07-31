#!/usr/bin/env python3
"""The image may consume only our own forks — enforced, not reviewed.

STANDING REPO INVARIANT: every OSS tool this image uses is forked into `vibeic/`
and tracked by the daily fork-gatekeeper tick. This is stated as what it
demonstrably IS rather than as a dated ruling: an earlier draft of this
docstring opened `Owner rule, 2026-07-29`, and no such decision is recorded
anywhere — searched before landing. A bare date reads as a citation and is not
one, which is the same shape as a comment claiming `OWNER RULING` with nothing
behind it (blocked on vibe-ic v1.7.96, same week). The invariant needs no
citation to bind: every pin in `docker-bake.hcl` names a `vibeic/` repo today,
`fork-gatekeeper/FORKS.json` is the tracked list, and the exceptions are named
and dated in `tools/PENDING_FORKS.json`. That rule was checkable only by reading a 605-line
Dockerfile, which means it held exactly as long as everyone who edited that file
remembered it. This makes it fail the build instead.

Two things are checked, and they are different:

1. **Every source we clone is `vibeic/`.** A `git clone` of an upstream URL means
   the binary in the image was built from source we do not control — the exact
   dependency the rule exists to retire.

2. **Every artefact we copy in is `ghcr.io/vibeic/eda-tool-*`.** A `COPY --from`
   naming any other registry pulls in a binary nobody on this side built.

`ARG` defaults are checked as well as the literal URLs, because a default that
points upstream is what a build with no `--build-arg` will actually use.

WHERE THIS RUNS. `.github/workflows/fork-only.yml` calls it on every push and PR,
but measured 2026-07-29 no workflow has ever executed on this repo (`actions/runs`
-> total_count 0) though Actions reports enabled. Until that changes, the thing
that actually runs this is the 05:30 fork-gatekeeper tick, which invokes it
against the checked-out repo and logs the verdict.

WHAT THIS DOES NOT CHECK, stated so a green run is not read as more than it is:
the base image itself (`hpretl/iic-osic-tools`) still supplies ~35 tools we do
not use and therefore do not fork. This checks what we BUILD and what we COPY,
not what the base happens to carry.

Exit: 0 clean, 1 violations found, 2 nothing was examined (which is not a pass).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

# Trailing slash optional: a bare `https://github.com/vibeic` (an org URL in a
# label or an ORG-scoped clone prefix) is ours too, and flagging it would
# teach the reader that this check cries wolf.
OURS_GIT = re.compile(r"https://github\.com/vibeic(?:/|$|\b)", re.I)
#: Any github.com URL that is not ours. Non-github sources (a project's own
#: gitlab, a tarball) are reported separately rather than silently allowed —
#: "not github" is not the same as "ours".
GIT_URL = re.compile(r"https://[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-/]+?(?:\.git)?(?=[\s\"']|$)")
COPY_FROM = re.compile(r"^\s*COPY\s+--from=([^\s]+)", re.I | re.M)
ARG_LINE = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.I | re.M)

#: Our own published artefact. `([:@].*)?` for the same reason ALLOWED_BASE below
#: carries it, and found the same way — by probing rather than by reading: pinned
#: by DIGEST, `ghcr.io/vibeic/eda-tool-yosys@sha256:...` was reported as "that
#: artefact was not built by us", about an artefact we built and published.
#:
#: It was latent only because every `ARG IMG_*` still names a TAG. The base moved
#: to a digest in 0.2.36 and this check punished that; the artefacts are the next
#: thing to be pinned that way, and the guard would have punished that too. Fixing
#: one of the two and leaving its twin is how the same bug gets rediscovered.
#:
#: What still bounds the suffix is the anchoring, not the suffix pattern: `^` plus
#: a literal `ghcr.io/vibeic/eda-tool-` prefix, and a name class that excludes `/`.
#: So `ghcr.io/vibeic/eda-tool-yosys/evil@…`, `…-tool-yosysEVIL@…` and
#: `evil.io/ghcr.io/vibeic/eda-tool-yosys@…` are all still rejected — probed in
#: `.github/workflows/fork-only.yml`, in both directions.
OURS_ARTEFACT = re.compile(r"^ghcr\.io/vibeic/eda-tool-[a-z0-9][a-z0-9._-]*([:@].*)?$", re.I)

#: A `COPY --from` naming a stage defined in the same file is internal wiring,
#: not an external artefact. Stage names carry no dots or slashes.
LOOKS_LIKE_STAGE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def pending() -> dict:
    """Named, dated exceptions — never a silent allowlist.

    A source can be legitimately un-forked for a while: GitHub throttles fork
    creation with a 403 that has nothing to do with rate limits or permissions,
    and it can persist for hours. Blocking every build on that would push someone
    toward the real damage — deleting the check.

    So the exception is allowed, and it is made expensive to keep: each entry
    carries an expiry, an EXPIRED entry FAILS, and every run prints what is
    outstanding. An exception with no deadline is how a temporary state becomes
    the permanent one.
    """
    import datetime, json
    f = Path(__file__).resolve().parent / "PENDING_FORKS.json"
    if not f.is_file():
        return {}
    try:
        entries = json.loads(f.read_text()).get("pending", [])
    except Exception as exc:                       # a malformed file is not a pass
        raise SystemExit("check_fork_only: %s is unreadable (%s); refusing to "
                         "treat that as 'no exceptions'" % (f, exc))
    today = datetime.date.today().isoformat()
    out = {}
    for e in entries:
        src = (e.get("source") or "").lower()
        if not src:
            continue
        out[src] = {"expired": (e.get("expires") or "") < today, **e}
    return out


#: A base a stage may legitimately be built ON — an OS or a vendor dev image.
#: The rule governs the TOOLS we build, not the distro underneath them.
#:
#: `(:.*)?` alone was wrong: it accepts `name:tag` and rejects `name@sha256:...`.
#: The base is now pinned by DIGEST, so a builder stage on it — `align-build` —
#: was reported as "not one of our artefacts" while being the same image the
#: composing stage uses. A guard that fires when the repo gets STRICTER is the
#: kind that gets switched off, and this one fired on exactly that.
#: `swift` added 2026-07-31 for tools/fault. It is the official Docker Library
#: Swift image and it is here for the same reason `ubuntu` is: it supplies a
#: COMPILER, and the rule above governs the tools we build, not the toolchain
#: that builds them. Fault itself is cloned from `vibeic/Fault` and nothing else.
#:
#: Stated plainly because it is the one case that is not purely a build-time
#: base: the artefact stage also copies the Swift RUNTIME libraries out of it
#: (libswiftCore, libFoundation, libdispatch, libBlocksRuntime, libicu*swift).
#: They are the language runtime a Swift binary cannot start without — the same
#: role libstdc++ and libc play for every C++ tool here, which ship from the
#: `ubuntu` bases already on this list. What must never come from here is a
#: TOOL, and none does.
ALLOWED_BASE = re.compile(
    r"^(scratch|ubuntu|debian|alpine|alpine/git|hpretl/iic-osic-tools"
    r"|swift|openroad/[a-z0-9._-]+)([:@].*)?$", re.I)


def stages(text: str) -> dict:
    """stage name -> the ref it is built FROM.

    The base matters, not just the name. `COPY --from=img-yosys` is safe only
    because `FROM ${IMG_YOSYS} AS img-yosys` resolves to one of our artefacts;
    the same COPY against `FROM someoneelse/yosys AS img-yosys` pulls a binary
    from outside the org while looking identical at the copy site. Recording
    only the names — which is what this did until 2026-07-29 — made every stage
    reference unconditionally trusted.
    """
    return {m.group(2).lower(): m.group(1)
            for m in re.finditer(r"^\s*FROM\s+(\S+)\s+AS\s+([A-Za-z0-9_.-]+)",
                                 text, re.I | re.M)}


def strip_comments(text: str) -> str:
    """A URL inside a comment documents history; it does not build anything.

    Checking commented-out lines would fail a Dockerfile for explaining what it
    used to do — and the explanation is usually the most valuable part.
    """
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in text.split("\n"))


def slug_of(url: str) -> str:
    """`https://github.com/Owner/Repo.git` -> `owner/repo`, the PENDING_FORKS key."""
    s = re.sub(r"^https://github\.com/", "", url, flags=re.I)
    return re.sub(r"\.git$", "", s).lower()


def expand(ref: str, args: dict) -> str | None:
    """Resolve ${VAR}/$VAR against this file's own ARG defaults.

    The default is what a build with no `--build-arg` actually uses, so the
    default IS the source — the same reasoning the ARG check below rests on.
    Returns None when a reference cannot be resolved from this file alone; the
    caller reports that rather than assuming it is harmless.
    """
    if "$" not in ref:
        return ref
    out, seen = ref, 0
    while "$" in out and seen < 8:                       # bounded: ARGs can chain
        m = re.search(r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)", out)
        if not m:
            break
        name = m.group(1) or m.group(2)
        if name not in args:
            return None
        out = out[:m.start()] + args[name] + out[m.end():]
        seen += 1
    return None if "$" in out else out


def check(path: Path, PENDING: dict) -> Tuple[List[str], int, List[str]]:
    raw = path.read_text(errors="replace")
    text = strip_comments(raw)
    known = stages(text)
    problems: List[str] = []
    deferred: List[str] = []
    examined = 0

    args = {m.group(1): m.group(2).strip("\"'")
            for m in ARG_LINE.finditer(text)}

    # Every stage base, whether or not anything copies from it.
    for name, base in known.items():
        rb = expand(base, args)
        if rb is None:
            examined += 1
            problems.append(
                f"{path}: FROM {base} AS {name}\n"
                f"    the base cannot be resolved from this file, so what the "
                f"stage is built on is decided elsewhere.")
        elif not (ALLOWED_BASE.match(rb) or OURS_ARTEFACT.match(rb)):
            examined += 1
            via = "" if rb == base else f" (via {base})"
            problems.append(
                f"{path}: FROM {rb}{via} AS {name}\n"
                f"    not one of our artefacts and not a plain OS/vendor base. "
                f"Anything copied out of this stage came from outside the org.")

    for m in COPY_FROM.finditer(text):
        ref = m.group(1)
        line = text[:m.start()].count("\n") + 1
        if ref.lower() in known:
            continue                        # base already checked in the loop above
        resolved = expand(ref, args)
        if resolved is not None and "/" not in resolved and ":" not in resolved:
            resolved = None          # a bare name that is not a stage: see below
        if resolved is None:
            # An unresolvable `--from` is NOT waved through. `COPY --from=img-yosys`
            # with no such stage is a *named build context*, supplied on the command
            # line — so the image it names is invisible here and could be anything.
            # Treating it as "probably a stage" is how the check would report clean
            # on a build that pulled a binary from outside the org.
            examined += 1
            problems.append(
                f"{path}:{line}: COPY --from={ref}\n"
                f"    neither a stage in this file nor an ARG defined here, so what "
                f"it resolves to is decided outside this file and cannot be checked. "
                f"Name the image through an ARG with a vibeic default.")
            continue
        examined += 1
        if not OURS_ARTEFACT.match(resolved):
            via = "" if resolved == ref else f" (via {ref})"
            problems.append(
                f"{path}:{line}: COPY --from={resolved}{via}\n"
                f"    that artefact was not built by us. Publish it as "
                f"ghcr.io/vibeic/eda-tool-<name>:<commit-sha> and copy from there.")

    for m in GIT_URL.finditer(text):
        url = m.group(0)
        if "github.com" not in url.lower():
            continue
        line = text[:m.start()].count("\n") + 1
        examined += 1
        if not OURS_GIT.match(url):
            p_entry = PENDING.get(slug_of(url))
            if p_entry and not p_entry["expired"]:
                deferred.append(
                    f"{path}:{line}: {url}  (pending fork, expires "
                    f"{p_entry.get('expires')}: {p_entry.get('reason')})")
                continue
            extra = ""
            if p_entry:
                extra = (f"\n    Its exception in PENDING_FORKS.json EXPIRED on "
                         f"{p_entry.get('expires')} — that deadline is the whole "
                         f"point of the file, so it is not extended silently.")
            problems.append(
                f"{path}:{line}: {url}\n"
                f"    building from a source we do not control. Fork it into "
                f"vibeic/, add it to fork-gatekeeper's FORKS.json, and point "
                f"this at the fork.{extra}")

    for m in ARG_LINE.finditer(text):
        name, val = m.group(1), m.group(2).strip('"\'')
        if "github.com" not in val.lower():
            continue
        line = text[:m.start()].count("\n") + 1
        examined += 1
        if not OURS_GIT.match(val):
            # Same dated exception as the clone check above. An ARG default and a
            # literal URL are the same statement about where the source comes
            # from; letting one be excepted and not the other would mean the fix
            # for a 403 depends on which line the URL happens to sit on.
            p_entry = PENDING.get(slug_of(val))
            if p_entry and not p_entry["expired"]:
                deferred.append(
                    f"{path}:{line}: ARG {name}={val}  (pending fork, expires "
                    f"{p_entry.get('expires')}: {p_entry.get('reason')})")
                continue
            extra = ""
            if p_entry:
                extra = (f"\n    Its exception in PENDING_FORKS.json EXPIRED on "
                         f"{p_entry.get('expires')}.")
            problems.append(
                f"{path}:{line}: ARG {name}={val}\n"
                f"    a default pointing upstream is what a build with no "
                f"--build-arg will use, so the default is the real source.{extra}")

    return problems, examined, deferred


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("paths", nargs="*", default=None,
                    help="Dockerfiles to check; defaults to ./Dockerfile and tools/*/Dockerfile")
    a = ap.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    files = [Path(p) for p in a.paths] if a.paths else (
        [root / "Dockerfile"] + sorted(root.glob("tools/*/Dockerfile")))
    files = [f for f in files if f.is_file()]

    if not files:
        print("check_fork_only: no Dockerfile examined — this is a gap in the "
              "check, not a pass", file=sys.stderr)
        return 2

    PENDING = pending()
    all_problems, all_deferred, total = [], [], 0
    for f in files:
        problems, examined, deferred = check(f, PENDING)
        total += examined
        all_problems += problems
        all_deferred += deferred

    if total == 0:
        print("check_fork_only: examined %d file(s) and found no source or "
              "artefact reference at all — the patterns no longer match this "
              "Dockerfile, so nothing was actually checked" % len(files),
              file=sys.stderr)
        return 2

    if all_problems:
        print("check_fork_only: %d violation(s) of the fork-only rule\n"
              % len(all_problems), file=sys.stderr)
        for p in all_problems:
            print("  " + p.replace("\n", "\n  "), file=sys.stderr)
        print("\n  Rule (owner, 2026-07-29): every OSS tool we use is forked into\n"
              "  vibeic/ and tracked by the daily fork-gatekeeper. The image\n"
              "  consumes our forks and nothing else.", file=sys.stderr)
        return 1

    if all_deferred:
        # Printed on a CLEAN run too. An outstanding exception that only appears
        # in failure output is one nobody reads until it is already permanent.
        print("check_fork_only: %d source(s) still upstream under a DATED "
              "exception:" % len(all_deferred))
        for d in all_deferred:
            print("  " + d)
        print("  Each fails this check once its expiry passes.")
    print("check_fork_only: %d reference(s) across %d file(s), %d ours, "
          "%d pending" % (total, len(files), total - len(all_deferred),
                          len(all_deferred)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
