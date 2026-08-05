"""Every path a `COPY` names must survive `.dockerignore`.

WHY THIS EXISTS (measured 2026-08-05)
=====================================
The compose added two files:

    COPY tools/refresh_versions_manifest.sh /vibeic/refresh_versions_manifest.sh
    COPY tools/tool_version_probes.tsv      /vibeic/tool_version_probes.tsv

`.dockerignore` excludes everything under `tools/` except `tools/pdk`, so
neither was in the build context:

    ERROR: failed to solve: failed to compute cache key:
      failed to calculate checksum of ref ...: "/tools/tool_version_probes.tsv": not found

It failed AFTER a ~20-minute OpenROAD rebuild, on the last stage of the compose,
and no version was cut.

WHAT MADE IT INVISIBLE
----------------------
The script itself was tested thoroughly -- three states, green and both reds,
against the real shipped image -- by MOUNTING it into a running container. That
answers "does the script work". It cannot answer "is the file in the build
context", because a mount does not go through `.dockerignore`. The verification
and the failure were about different things, which is the recurring shape here:
a check that validates something ADJACENT to the claim.

The lesson already existed and was skipped: probe a Dockerfile change with a
minimal BUILD, not with a container run. This test is the cheap version of that
probe -- it answers the same question in milliseconds, for every COPY, forever.

WHAT THIS ASSERTS
-----------------
Not "these two files are listed" -- that is the fix, and pinning the fix would
neither have caught the original nor catch the next COPY someone adds. It parses
every `COPY <src>` in the composing Dockerfile and requires the source to be
included by the `.dockerignore` rules, evaluated in order, the way Docker
evaluates them: last matching pattern wins.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

#: `COPY --from=<stage>` copies out of another build stage, not the context, so
#: `.dockerignore` does not apply to it.
_COPY = re.compile(r"^COPY\s+(?P<flags>(?:--\S+\s+)*)(?P<srcs>.+?)\s+(?P<dst>\S+)\s*$",
                   re.M)


def _rules():
    """(negated, pattern) in file order. Comments and blanks dropped."""
    out = []
    for ln in DOCKERIGNORE.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        neg = ln.startswith("!")
        out.append((neg, ln[1:] if neg else ln))
    return out


def _included(path: str, rules) -> bool:
    """Docker semantics: start included, apply every rule in order, last wins."""
    included = True
    for neg, pat in rules:
        if fnmatch(path, pat) or fnmatch(path, pat.rstrip("/") + "/*"):
            included = neg
        # A directory pattern also governs paths beneath it.
        elif path.startswith(pat.rstrip("/*") + "/"):
            included = neg
    return included


def _context_copy_sources():
    """Every COPY source that comes from the build context."""
    out = []
    for m in _COPY.finditer(DOCKERFILE.read_text()):
        if "--from=" in m.group("flags"):
            continue
        for src in m.group("srcs").split():
            out.append(src)
    return out


def test_the_dockerfile_has_context_copies_at_all():
    """Control: if the parser matched nothing, every other test here passes
    vacuously and would keep passing after someone broke the context."""
    srcs = _context_copy_sources()
    assert srcs, "no context COPY found — the parser is broken, not the Dockerfile"


@pytest.mark.parametrize("src", sorted(set(_context_copy_sources())))
def test_every_copy_source_survives_dockerignore(src):
    rules = _rules()
    assert _included(src, rules), (
        f"the composing Dockerfile does `COPY {src}`, but .dockerignore "
        f"excludes it, so it is not in the build context. The compose will die "
        f'with `"/{src}": not found` — and it dies on the stage that names it, '
        f"which for the tool-manifest refresh was after a 20-minute OpenROAD "
        f"rebuild. Add `!{src}` to .dockerignore.")


@pytest.mark.parametrize("src", sorted(set(_context_copy_sources())))
def test_every_copy_source_exists_on_disk(src):
    """A path that survives .dockerignore but does not exist fails identically."""
    assert (ROOT / src).exists(), (
        f"`COPY {src}` names a path that is not in the repo")


def test_this_check_can_see_an_excluded_source():
    """Bidirectional control.

    A rule evaluator that returns True for everything passes every test above
    while checking nothing. This constructs the exact defect that occurred and
    requires it to be caught.
    """
    rules = [(False, "*"), (False, "!Dockerfile"), (False, "tools/*"),
             (True, "tools/pdk")]
    # As it was: everything under tools/ excluded except tools/pdk.
    rules = [(False, "*"), (True, "Dockerfile"), (True, "tools"),
             (False, "tools/*"), (True, "tools/pdk")]
    assert not _included("tools/tool_version_probes.tsv", rules), (
        "the evaluator says the file WAS in the context under the rules that "
        "excluded it — it cannot detect the defect it exists for")
    assert _included("tools/pdk/local_mods.json", rules), (
        "the evaluator excludes tools/pdk, which was always allowed — it would "
        "report a defect that is not there")
