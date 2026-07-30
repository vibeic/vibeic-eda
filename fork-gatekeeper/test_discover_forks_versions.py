#!/usr/bin/env python3
"""vibeic-eda#31 — the version list that could not see the versions.

`_releases()` returned as soon as the RELEASE list was non-empty, so the tag
fallback only ever fired for a project that had never published a release at
all. A project that published one release and then moved to tags stayed pinned
to it forever.

OpenROAD is that project: one release, `v0.9.0-beta` from 2020-07-06, while it
ships quarterly tags — our own binary is `26Q3-951-g92b079b47a`. The ledger
reported that 2020 tag as the latest upstream version, our pin was six years
ahead of it, `behind_releases` came out 0, and `assess_release` skipped the tool
on every tick since the fork was made. Nothing errored. The tool was simply
absent from the reports, and an absent tool looks exactly like a tool with
nothing to adopt.

These pin the merge, and — more importantly — the ORDER, because the caller
takes `rels[0]` and runs an ancestry compare against it. A wrong first element
is not a cosmetic problem; it is the whole verdict.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("df", HERE / "discover_forks.py")
df = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df)


def _stub(monkeypatch, *, releases=None, tags=None):
    """Substitute both sources. `releases` is REST shape, `tags` is ours."""
    def fake_gh(path):
        if "/releases" in path:
            return [{"tag_name": t, "published_at": f"{d}T00:00:00Z"}
                    for t, d in (releases or [])]
        return {"_err": "unexpected path"}
    monkeypatch.setattr(df, "gh", fake_gh)
    monkeypatch.setattr(df, "_tags_by_date",
                        lambda up, limit=30: [{"tag": t, "date": d}
                                              for t, d in (tags or [])])


def test_a_stale_release_does_not_hide_a_newer_tag(monkeypatch):
    """THE defect, in miniature: OpenROAD's shape."""
    _stub(monkeypatch,
          releases=[("v0.9.0-beta", "2020-07-06")],
          tags=[("26Q3", "2026-06-30"), ("26Q2", "2026-04-07"),
                ("v0.9.0-beta", "2020-07-06")])
    got = df._releases("The-OpenROAD-Project/OpenROAD")
    assert got[0]["tag"] == "26Q3", \
        f"latest resolved to {got[0]['tag']}; the caller compares against this"
    assert got[0]["date"] == "2026-06-30"


def test_a_project_that_only_publishes_releases_is_unchanged(monkeypatch):
    """No regression for the 12 upstreams whose releases are current."""
    _stub(monkeypatch,
          releases=[("8.3.678", "2026-07-27"), ("8.3.677", "2026-07-26")],
          tags=[("8.3.678", "2026-07-27")])
    got = df._releases("RTimothyEdwards/magic")
    assert [r["tag"] for r in got] == ["8.3.678", "8.3.677"]


def test_a_project_that_only_tags_still_works(monkeypatch):
    """The case the old fallback DID handle — verilator, ngspice, netgen,
    xschem, sby, ALIGN-pdk-sky130. It must keep working."""
    _stub(monkeypatch, releases=[],
          tags=[("v5.050", "2026-07-20"), ("v5.048", "2026-05-02")])
    assert [r["tag"] for r in df._releases("verilator/verilator")] == \
           ["v5.050", "v5.048"]


def test_the_same_version_from_both_sources_appears_once(monkeypatch):
    """A release and its tag are one version, not two. Duplicates would inflate
    `behind_releases`, which is a COUNT the report acts on."""
    _stub(monkeypatch,
          releases=[("v1.0", "2026-01-01")],
          tags=[("v1.0", "2025-12-31")])
    got = df._releases("x/y")
    assert len(got) == 1
    assert got[0]["date"] == "2026-01-01", \
        "the release's publication date should win over the commit date"


def test_an_undated_entry_never_takes_the_latest_slot(monkeypatch):
    """`rels[0]` drives the ancestry compare. An entry with no date is not
    evidence of being newest, and letting it sort first would put an arbitrary
    tag in the slot the verdict depends on."""
    _stub(monkeypatch,
          releases=[("weird", "")],
          tags=[("26Q3", "2026-06-30")])
    got = df._releases("x/y")
    assert got[0]["tag"] == "26Q3", \
        f"an undated entry won the latest slot: {got}"


def test_both_sources_silent_yields_an_empty_list(monkeypatch):
    """Not a crash, and not a fabricated version. The caller records
    `upstream_latest_release=None` — a missing value rather than a reassuring
    one."""
    _stub(monkeypatch, releases=[], tags=[])
    assert df._releases("x/y") == []


def test_tags_are_requested_even_when_releases_answered(monkeypatch):
    """The regression that would silently restore the bug: an early return once
    the release list is non-empty. Nothing else in these tests would catch it if
    the merge happened to agree."""
    seen = {"tags": False}

    def fake_gh(path):
        return [{"tag_name": "old", "published_at": "2020-01-01T00:00:00Z"}]

    def fake_tags(up, limit=30):
        seen["tags"] = True
        return []

    monkeypatch.setattr(df, "gh", fake_gh)
    monkeypatch.setattr(df, "_tags_by_date", fake_tags)
    df._releases("x/y")
    assert seen["tags"], "the tag source was skipped because releases answered"


def test_tags_by_date_survives_a_failing_gh(monkeypatch):
    """A GraphQL failure must degrade to "no tags", never raise into the tick."""
    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"
    monkeypatch.setattr(df.subprocess, "run", lambda *a, **k: R())
    assert df._tags_by_date("x/y") == []


def test_tags_by_date_reads_an_annotated_tag(monkeypatch):
    """A lightweight tag points at a Commit; an annotated tag points at a Tag
    that points at the Commit. Handling only the first silently drops every
    annotated tag, which is how most projects tag releases."""
    class R:
        returncode = 0
        stderr = ""
        stdout = ('{"data":{"repository":{"refs":{"nodes":['
                  '{"name":"light","target":{"committedDate":"2026-01-02T00:00:00Z"}},'
                  '{"name":"annot","target":{"target":'
                  '{"committedDate":"2026-03-04T00:00:00Z"}}}]}}}}')
    monkeypatch.setattr(df.subprocess, "run", lambda *a, **k: R())
    got = {t["tag"]: t["date"] for t in df._tags_by_date("x/y")}
    assert got == {"light": "2026-01-02", "annot": "2026-03-04"}


# --------------------------------------------------------------------------
# vibeic-eda#31, second half — the range needs two ENDS, not just a latest.
#
# Driven through the real `assess()` with the fixture style test_assess.py
# already uses (GK_STATE_DIR + a ledger on disk + stubbed layers), because the
# thing under test is WHICH RANGE assess picks, and a hand-called helper would
# not exercise that choice.
# --------------------------------------------------------------------------

import importlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

import assess_release as A  # noqa: E402


def _fixture(tmp: Path, **led_over):
    """assess() wired to stub layers; returns the list the range lands in."""
    os.environ["GK_STATE_DIR"] = str(tmp)
    importlib.reload(A)
    led = {"tool": "OpenROAD", "integrated": True,
           "upstream": "The-OpenROAD-Project/OpenROAD",
           "upstream_default_branch": "master",
           "pinned_ref_full": "9" * 40,
           "base_release": "v0.9.0-beta",
           "upstream_latest_release": "v0.9.0-beta",
           "behind_releases": 0, "behind_commits": 7, "role": "PnR"}
    led.update(led_over)
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / "ledger" / f"{led['tool']}.json").write_text(json.dumps(led))

    seen = []
    A.upstream_commits = lambda up, base, new: (seen.append((base, new)) or ([], []))
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: set()
    A.clean_cherrypick = lambda *a: True
    A.classify_commits = lambda tool, role, commits: {}
    A._confirm_candidates = lambda *a, **k: {}
    return led, seen


def test_an_empty_release_range_falls_back_to_commits():
    """THE second defect. `base_release == upstream_latest_release` collapses the
    range onto one tag, so the assessment covers an empty diff and the fork reads
    as clean while upstream master has moved. OpenROAD sat in exactly that state:
    both ends `v0.9.0-beta` (2020-07-06), master moving daily."""
    with tempfile.TemporaryDirectory() as d:
        led, seen = _fixture(Path(d))
        A.assess("OpenROAD")
    assert seen, "assess() never asked for a commit range"
    base, new = seen[0]
    assert (base, new) != ("v0.9.0-beta", "v0.9.0-beta"), \
        "the range is still a single point; the assessment would cover nothing"
    assert base == "9" * 40, f"expected our pinned ref as base, got {base}"
    assert new == "master", f"expected upstream default branch as head, got {new}"


def test_a_real_release_range_is_left_alone():
    """The twelve upstreams with current releases must keep the RELEASE range —
    magic ships daily and its tag range is the right question for it."""
    with tempfile.TemporaryDirectory() as d:
        led, seen = _fixture(Path(d), tool="magic", upstream="RTimothyEdwards/magic",
                             base_release="8.3.674", upstream_latest_release="8.3.678",
                             behind_releases=4, behind_commits=108)
        A.assess("magic")
    assert seen and seen[0] == ("8.3.674", "8.3.678"), \
        f"a genuine release range was overwritten with a commit range: {seen}"


def test_without_a_pinned_ref_no_range_is_invented():
    """No pinned ref means no base. Falling through to the existing
    missing-base error is correct; substituting something plausible is not."""
    with tempfile.TemporaryDirectory() as d:
        led, seen = _fixture(Path(d), pinned_ref_full=None,
                             base_release=None, upstream_latest_release=None)
        r = A.assess("OpenROAD")
    assert not seen, f"a range was built without a pinned ref: {seen}"
    assert "error" in r, f"expected the missing-base error, got {r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# pin parsing — the ref sits on either side of the URL (vibeic-eda#32)
# --------------------------------------------------------------------------

_BRANCH_FORM = '''
ARG ORFS_REF=v3.0
RUN git clone --depth 1 --branch ${ORFS_REF} --filter=blob:none --sparse \\
      https://github.com/vibeic/OpenROAD-flow-scripts.git /orfs \\
 && git -C /orfs sparse-checkout set flow/platforms/nangate45
'''

_CHECKOUT_FORM = '''
ARG MAGIC_REPO=https://github.com/vibeic/magic.git
ARG MAGIC_REF=9d3ed4b16b5e5d6570846b448b89ed7d953cd14b
RUN git clone "${MAGIC_REPO}" /magic \\
 && cd /magic && git checkout ${MAGIC_REF} \\
 && make
'''

_THREE_IN_ONE_RUN = '''
ARG ASAP7SC_REF=main
ARG ASAP7PDK_REF=main
ARG ASAP7KL_REF=main
RUN git clone --depth 1 --branch ${ASAP7SC_REF} --sparse \\
      https://github.com/vibeic/asap7sc7p5t_28.git /a7sc \\
 && git clone --depth 1 --branch ${ASAP7PDK_REF} --sparse \\
      https://github.com/vibeic/asap7_pdk_r1p7.git /a7pdk \\
 && git clone --depth 1 --branch ${ASAP7KL_REF} \\
      https://github.com/vibeic/ASAP7_for_KLayout.git /a7kl
'''

_PROVENANCE_TAIL = '''
ARG MAGIC_REPO=https://github.com/vibeic/magic.git
ARG MAGIC_REF=aaaaaaaaaaaa
ARG NETGEN_REPO=https://github.com/vibeic/netgen.git
ARG NETGEN_REF=bbbbbbbbbbbb
RUN git clone "${MAGIC_REPO}" /magic && cd /magic && git checkout ${MAGIC_REF}
RUN git clone "${NETGEN_REPO}" /netgen && cd /netgen && git checkout ${NETGEN_REF}
RUN printf '{"repo":"%s","ref":"%s","netgen_repo":"%s","netgen_ref":"%s"}' \\
      "${MAGIC_REPO}" "${MAGIC_REF}" "${NETGEN_REPO}" "${NETGEN_REF}" > /p.json
'''


def test_the_branch_form_is_parsed():
    """`--branch ${REF}` puts the ref BEFORE the URL. Searching only forward
    missed it entirely, and OpenROAD-flow-scripts — which supplies
    /foss/pdks/nangate45 and /foss/pdks/asap7 to the shipped image — read as a
    fork with no pin at all (vibeic-eda#32)."""
    pins = df.parse_dockerfile_pins(_BRANCH_FORM)
    assert pins.get("openroad-flow-scripts", {}).get("arg") == "ORFS_REF"


def test_the_checkout_form_still_parses():
    """The eight pins that already worked. Three attempts at "nearest ref"
    fixed the branch form and broke this one."""
    pins = df.parse_dockerfile_pins(_CHECKOUT_FORM)
    assert pins.get("magic", {}).get("arg") == "MAGIC_REF"


def test_three_clones_in_one_run_each_get_their_own_ref():
    """The mispairing that a distance heuristic cannot avoid: looking forward
    gave asap7sc7p5t_28 the pin of asap7_pdk_r1p7, looking backward gave it the
    pin of the clone before. A WRONG pin is worse than a missing one — the row
    reads as tracked."""
    pins = df.parse_dockerfile_pins(_THREE_IN_ONE_RUN)
    assert pins.get("asap7sc7p5t_28", {}).get("arg") == "ASAP7SC_REF"
    assert pins.get("asap7_pdk_r1p7", {}).get("arg") == "ASAP7PDK_REF"
    assert pins.get("asap7_for_klayout", {}).get("arg") == "ASAP7KL_REF"


def test_a_provenance_line_naming_both_refs_does_not_steal_the_pin():
    """`tools/lvs/Dockerfile` writes both tools' repo+ref into one printf. An
    instruction-wide search let magic's clause run into it and pick up
    NETGEN_REF."""
    pins = df.parse_dockerfile_pins(_PROVENANCE_TAIL)
    assert pins.get("magic", {}).get("arg") == "MAGIC_REF"
    assert pins.get("netgen", {}).get("arg") == "NETGEN_REF"


_CHECKOUT_WITH_DECOY = '''
ARG TOOL_REPO=https://github.com/vibeic/tool.git
ARG DECOY_REF=deadbeef
ARG TOOL_REF=cafebabe
RUN git clone "${TOOL_REPO}" /tool \\
 && echo "unrelated ${DECOY_REF} mentioned first" \\
 && cd /tool && git checkout ${TOOL_REF}
'''


def test_checkout_wins_over_an_earlier_unrelated_ref():
    """The `checkout` branch must be doing the work, not the generic
    forward-scan fallback behind it.

    Removing the checkout pattern left the whole suite green, because the
    fallback picks the first `${*_REF}` after the URL and that happened to be
    the right one in every fixture. Here it is not: a decoy ref appears first,
    so only a parser that looks for `checkout` specifically gets this right.
    """
    pins = df.parse_dockerfile_pins(_CHECKOUT_WITH_DECOY)
    assert pins.get("tool", {}).get("arg") == "TOOL_REF", \
        "the generic forward scan grabbed the decoy"


_REPO_VAR_WITH_SUBMODULES = '''
ARG HOST_REPO=https://github.com/vibeic/Host.git
ARG HOST_REF=1111111111111111111111111111111111111111
RUN git clone "${HOST_REPO}" /src \\
 && cd /src && git checkout ${HOST_REF} \\
 && git submodule update --init --recursive --depth 1 \\
 && ./build.sh
RUN mkdir -p /vibeic/provenance \\
 && printf '{"tool":"host","repo":"%s","ref":"%s"}' \\
      "${HOST_REPO}" "${HOST_REF}" > /vibeic/provenance/host.json
'''


def test_clone_flags_survive_a_later_provenance_line_naming_the_same_url():
    """After `${X_REPO}` substitution the URL appears several times — the ARG
    that defines it, the RUN that clones, and the provenance printf that records
    it — and the parse loop assigns on every match, so the LAST wins.

    For `tools/openroad/Dockerfile` the last is the printf, which has neither
    `git clone` nor `submodule` in it:

        1252  ARG OPENROAD_REPO=…
        2885  RUN git clone … && git submodule update --init --recursive
        5279  RUN printf '{"tool":"openroad","repo":"%s"…'     <- won

    So `submodules` read False for a build that fetches them, and
    `expand_vendored_pins` never fired for the fork it was written for: OpenSTA,
    vendored at src/sta, in the image while the ledger called it absent
    (vibeic-eda#8, #32). This fixture carries the printf, which the earlier one
    did not — without it the test passed against the bug.
    """
    pins = df.parse_dockerfile_pins(_REPO_VAR_WITH_SUBMODULES)
    host = pins.get("host") or {}
    assert host.get("submodules") is True, \
        "a provenance line mentioning the URL overwrote the clone step's flags"
    assert host.get("recursive") is True


_NO_SUBMODULE_INIT = '''
ARG PLAIN_REPO=https://github.com/vibeic/Plain.git
ARG PLAIN_REF=2222222222222222222222222222222222222222
RUN git clone "${PLAIN_REPO}" /src \\
 && cd /src && git checkout ${PLAIN_REF} \\
 && make
'''


def test_a_clone_without_submodule_init_stays_false():
    """…or the test above is met by hardcoding True. A declared-but-never-fetched
    submodule is correctly NOT integrated, which is the whole reason the flag
    exists rather than being assumed."""
    pins = df.parse_dockerfile_pins(_NO_SUBMODULE_INIT)
    assert (pins.get("plain") or {}).get("submodules") is False
