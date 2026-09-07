#!/usr/bin/env python3
"""A failed round's evidence was destroyed by the next round, on a fixed schedule.

Every stage of `run_tick` writes to a FIXED path in `$GK_STATE_DIR` with `>`:
`source-guards.txt`, `daily-merge.txt`, `vendored-closure.txt`,
`fork-selftests.txt`. `daily_release` already learned this — vibeic-eda#96 grew
`failed-releases/` precisely because `daily-release.txt` was overwritten before
anyone read it — but only for its own output, and only inside the branch where a
build actually ran.

The refusal branch, the one that stops the round BEFORE the build, retained
nothing. Measured: the 2026-08-25, 2026-09-07 and 2026-09-08 rounds all exited 4
(`daily_merge` refused). Each opened an alert PR whose only instruction was to
read a build archive that class never writes, and by the time a human looked, the
record that held the answer — which fork refused, and whether it was the same fork
as yesterday — had been overwritten or was hours from it. Three rounds, and it is
still not answerable from outside the build host.

THE PROPERTY UNDER TEST IS NOT "the copy happened". It is that the archive
distinguishes a file the round never wrote from a file this program failed to
copy, because those two look identical in a directory listing and the whole
2026-09-08 confusion was a reader unable to tell an absent record from a failed
one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import archive_round_evidence as A  # noqa: E402

NAMES = ("source-guards.txt", "daily-merge.txt", "daily-merge.json",
         "fork-selftests.txt")


@pytest.fixture()
def state(tmp_path):
    d = tmp_path / "gk-state"
    d.mkdir()
    (d / "daily-merge.txt").write_text(
        "daily_merge: 38 fork(s), 1 merged, 2 need a human\n", encoding="utf-8")
    (d / "daily-merge.json").write_text('{"program": "daily_merge"}\n',
                                        encoding="utf-8")
    (d / "source-guards.txt").write_text("", encoding="utf-8")
    return d


def test_the_evidence_is_retained_byte_for_byte(state):
    rc, dest, _ = A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    assert rc == A.RC_OK
    assert (dest / "daily-merge.txt").read_text(encoding="utf-8") == (
        (state / "daily-merge.txt").read_text(encoding="utf-8"))
    assert (dest / "daily-merge.json").read_bytes() == (
        (state / "daily-merge.json").read_bytes())


def test_the_originals_are_left_where_the_round_put_them(state):
    """This runs INSIDE the round, before the stages that read these files."""
    A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    assert (state / "daily-merge.txt").is_file()
    assert (state / "daily-merge.json").is_file()


def test_a_file_the_round_never_wrote_is_recorded_as_absent(state):
    """THE POINT. `fork-selftests.txt` was asked for and does not exist. An
    archive that silently omits it cannot be distinguished from an archive that
    failed to copy it, and 'the record is missing' was the entire content of
    three consecutive alerts."""
    rc, dest, rows = A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    assert rc == A.RC_OK
    manifest = (dest / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "ABSENT\tfork-selftests.txt" in manifest, manifest
    assert not (dest / "fork-selftests.txt").exists()
    assert dict(rows)["fork-selftests.txt"] == "ABSENT"


def test_an_empty_file_is_copied_not_called_absent(state):
    """An empty `source-guards.txt` is what a CLEAN guard sweep leaves. Reading
    it as absent would report the one stage that passed as unmeasured."""
    _, dest, rows = A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    assert (dest / "source-guards.txt").is_file()
    assert dict(rows)["source-guards.txt"].startswith("copied 0")
    manifest = (dest / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "ABSENT\tsource-guards.txt" not in manifest, manifest


def test_every_asked_for_name_appears_in_the_manifest(state):
    _, dest, rows = A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    manifest = (dest / "MANIFEST.txt").read_text(encoding="utf-8")
    for n in NAMES:
        assert f"\t{n}" in manifest, (n, manifest)
    assert len(rows) == len(NAMES)


def test_a_missing_state_dir_is_refused_not_reported_clean(tmp_path):
    """Nothing could be archived. That is not the same as nothing needing to be,
    and a program that returns 0 here teaches the round it is protected when it
    is not."""
    rc, dest, rows = A.archive(tmp_path / "nope", NAMES)
    assert rc == A.RC_NOSTATE
    assert dest is None and rows == []


def test_retention_keeps_the_newest_rounds_and_prunes_the_rest(state):
    for day in range(1, 9):
        A.archive(state, NAMES, keep=5, stamp=f"2026-09-0{day}T053001Z")
    kept = sorted(d.name for d in (state / A.ARCHIVE_DIRNAME).iterdir())
    assert len(kept) == 5, kept
    assert kept[0].startswith("2026-09-04"), kept
    assert kept[-1].startswith("2026-09-08"), kept


def test_five_rounds_is_enough_to_see_the_same_stage_four_mornings_running(state):
    """Retention exists to answer transient-versus-repeating, which needs more
    than one morning. A `keep` that cannot hold a work week answers nothing."""
    assert A.DEFAULT_KEEP >= 5


def test_a_name_that_escapes_the_state_dir_is_refused(state):
    """The caller assembles the list, but this program is the one that writes into
    a retained directory; it does not get to be the reason something outside the
    state dir is copied."""
    _, dest, rows = A.archive(state, ("../daily-merge.txt", "a/b.txt"),
                              stamp="2026-09-08T053001Z")
    assert dict(rows)["../daily-merge.txt"] == "REFUSED-not-a-basename"
    assert dict(rows)["a/b.txt"] == "REFUSED-not-a-basename"
    assert not (dest / "daily-merge.txt").exists()


def test_the_archive_is_not_the_directory_the_release_path_uses(state):
    """`failed-releases/` is the build archive and is read by a human looking for
    a build log. Mixing a blocked round's stage records into it would make the one
    directory answer two different questions."""
    _, dest, _ = A.archive(state, NAMES, stamp="2026-09-08T053001Z")
    assert "failed-releases" not in str(dest)
    assert A.ARCHIVE_DIRNAME == "failed-rounds"


def test_the_cli_reports_what_it_retained_and_what_was_absent(state, capsys):
    rc = A.main(["--state-dir", str(state), "--stamp", "2026-09-08T053001Z",
                 *NAMES])
    out = capsys.readouterr().out
    assert rc == A.RC_OK
    assert "3 of 4 evidence file(s)" in out, out
    assert "ABSENT\tfork-selftests.txt" in out, out


def test_the_cli_refuses_a_state_dir_that_is_not_there(tmp_path, capsys):
    rc = A.main(["--state-dir", str(tmp_path / "nope"), *NAMES])
    assert rc == A.RC_NOSTATE
    assert "does not exist" in capsys.readouterr().out


# --- the refusal itself, said positively -------------------------------------

import record_blocked_round as REC  # noqa: E402


def test_the_record_says_the_release_was_not_attempted():
    """The one fact the absence of `daily-release.json` could not state."""
    body = REC.record({"merge_rc": 1})
    assert body["state"] == "BLOCKED"
    assert body["release_attempted"] is False


def test_the_record_names_every_blocking_stage_not_just_the_first():
    body = REC.record({"guard_rc": 0, "merge_rc": 1, "vendored_rc": 0,
                       "selftest_rc": 2})
    named = {r["stage"]: r["rc"] for r in body["blocked_by"]}
    assert named == {"daily_merge": 1, "fork self-checks": 2}, named


def test_a_green_stage_is_not_listed_as_blocking():
    body = REC.record({"merge_rc": 1})
    assert [r["stage"] for r in body["blocked_by"]] == ["daily_merge"]


def test_each_stage_carries_the_evidence_file_it_leaves(tmp_path):
    body = REC.record({"merge_rc": 1, "guard_rc": 1})
    ev = {r["stage"]: r["evidence"] for r in body["blocked_by"]}
    assert ev["daily_merge"] == "daily-merge.txt"
    assert ev["source guards"] == "source-guards.txt"


def test_nothing_blocking_is_refused_not_written(tmp_path, capsys):
    """A caller that reaches this program with four zeroes has a bug. A record
    saying "BLOCKED by nothing" would hide it behind a file that looks fine."""
    rc = REC.main(["--state-dir", str(tmp_path)])
    assert rc == REC.RC_NOTHING_BLOCKING
    assert not (tmp_path / REC.RECORD_NAME).exists()
    assert "nothing refused" in capsys.readouterr().out


def test_the_record_is_written_where_the_round_keeps_its_state(tmp_path):
    rc = REC.main(["--state-dir", str(tmp_path), "--merge-rc", "1",
                   "--round-at", "2026-09-08T05:30:01+08:00"])
    assert rc == REC.RC_OK
    import json as _json
    body = _json.loads((tmp_path / REC.RECORD_NAME).read_text(encoding="utf-8"))
    assert body["round_at"] == "2026-09-08T05:30:01+08:00"
    assert body["blocked_by"][0]["rc_var"] == "merge_rc"


def test_the_record_is_not_named_like_a_release_record():
    """Every consumer of `daily-release.json` reads it as a release record, and a
    release record describing a release that never happened is the false
    certificate this whole file is about."""
    assert REC.RECORD_NAME != "daily-release.json"
    assert "release" not in REC.RECORD_NAME
