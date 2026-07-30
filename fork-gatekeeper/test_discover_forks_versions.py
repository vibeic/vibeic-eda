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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
