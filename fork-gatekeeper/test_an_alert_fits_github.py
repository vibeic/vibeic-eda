"""An alert GitHub would not accept was an alert nobody saw.

2026-09-30: the release record carried an 85,923-character registry reply twice
-- `failure.summary` and `unmeasured[0].detail` -- and `check_release_failed`
quoted both in full.  `gh pr create` answered

    GraphQL: Body is too long (maximum is 65536 characters)

the script printed [COULD NOT RAISE] and returned 1, and the one alarm for a
failed release was never raised.  The record had already said everything a
triager needs in its short lines: stage, category, subject, retryable.

So the body is bounded (at most BODY_CAP, under GitHub's limit with margin),
each line is bounded with a count of what was elided, and the block is bounded
with a count of the lines left out.  The summary -- the PR title -- is untouched,
and so is the body of any record that already fitted.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_release_failed as C  # noqa: E402

ROUND_AT = "2026-09-30T05:30:01+08:00"
GITHUB_LIMIT = 65536
SUBJECT = "ghcr.io/vibeic/vibeic-eda:" + (HERE.parent / "VERSION").read_text().strip()
HUGE = ("registry manifest unmeasured: unsupported manifest format: &{{{2 "
        "application/vnd.oci.image.manifest.v1+json} " + " 32" * 70_000 + " 125]}")


def _raise(monkeypatch, tmp_path, record: dict):
    """Run the checker as the round does; capture what it would send."""
    path = tmp_path / "daily-release.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    sent = []

    def escalate(summary, body, **_kwargs):
        sent.append((summary, body))
        return True, "captured"

    monkeypatch.setattr(C, "_default_escalate", escalate)
    rc = C.main(["--tick-exit", "5", "--round-at", ROUND_AT,
                 "--release-json", str(path)])
    assert rc == C.RC_ESCALATED
    assert len(sent) == 1
    return sent[0]


def _record(detail: str, *, obstacles: int = 1) -> dict:
    return {
        "program": "daily_release",
        "status": "unmeasured",
        "exit_code": 1,
        "failure": {
            "stage": "noop_publication",
            "category": "version_manifest_unmeasurable",
            "retryable": True,
            "subject": SUBJECT,
            "summary": detail,
        },
        "unmeasured": [{
            "stage": "noop_publication",
            "category": "version_manifest_unmeasurable",
            "subject": SUBJECT,
            "detail": detail,
        } for _ in range(obstacles)],
    }


def test_a_200k_detail_still_fits_a_github_body(monkeypatch, tmp_path):
    """THE REGRESSION, at twice the observed size per field."""
    assert len(HUGE) > 200_000
    summary, body = _raise(monkeypatch, tmp_path, _record(HUGE))

    assert len(body) < GITHUB_LIMIT
    assert len(body) <= 60_000
    for line in ("failure.stage: noop_publication",
                 "failure.category: version_manifest_unmeasurable",
                 f"failure.subject: {SUBJECT}",
                 "failure.retryable: True",
                 "unmeasured[0].category: version_manifest_unmeasurable",
                 f"unmeasured[0].subject: {SUBJECT}"):
        assert line in body.splitlines(), line
    # What failed survives the cut; how much was cut is stated.
    assert "failure.summary: registry manifest unmeasured: unsupported manifest format" in body
    assert re.search(r"\.\.\. \[\d+ chars elided\]", body)
    # The title is not the body's business.
    assert summary == "release failed on the 2026-09-30 round (run_tick exit 5)"


def test_a_long_line_is_capped_and_says_how_much(monkeypatch, tmp_path):
    _summary, body = _raise(monkeypatch, tmp_path, _record(HUGE))
    line = next(ln for ln in body.splitlines()
                if ln.startswith("failure.summary: "))
    full = f"failure.summary: {HUGE}"

    assert len(line) <= 2000
    elided = int(re.search(r"\.\.\. \[(\d+) chars elided\] \.\.\. ", line).group(1))
    kept = len(re.sub(r" \.\.\. \[\d+ chars elided\] \.\.\. ", "", line))
    assert kept + elided == len(full)
    assert full.startswith(line.split(" ... [", 1)[0])
    assert full.endswith(line.rsplit("] ... ", 1)[1])


def test_many_obstacles_are_counted_not_silently_dropped(monkeypatch, tmp_path):
    """The block has a cap too; what it leaves out is counted, and the
    failure's own lines -- emitted first -- are never what is left out."""
    _summary, body = _raise(monkeypatch, tmp_path, _record(HUGE, obstacles=200))

    assert len(body) <= 60_000
    lines = body.splitlines()
    assert "failure.category: version_manifest_unmeasurable" in lines
    assert f"failure.subject: {SUBJECT}" in lines
    assert "failure.stage: noop_publication" in lines
    assert "unmeasured[0].category: version_manifest_unmeasurable" in lines
    note = next(ln for ln in lines if "more line(s)" in ln)
    assert re.match(r"\.\.\. \[\d+ more line\(s\), \d+ chars elided; ", note)
    assert "unmeasured[199]." not in body


def test_a_normal_record_body_is_unchanged(monkeypatch, tmp_path):
    """Guard on content: a record that already fitted is quoted verbatim, with
    no marker, exactly where the body always put it."""
    record = _record("registry presence unmeasured: net/http: TLS handshake timeout")
    _summary, body = _raise(monkeypatch, tmp_path, record)
    detail = C._detail_from(record)

    assert "elided" not in body
    assert ("## What the release said\n\n```\n" + detail.strip()
            + "\n```\n\n## Why this is a PR and not a page banner\n\n") in body
    assert body.startswith(
        f"`run_tick` exited **5** (state `ran`) on the {ROUND_AT} round, so no "
        "image was published and `build_page` was skipped.\n\n")
    assert body.endswith(
        "---\n_Opened by the round itself. Operational alert, not an auto-merge._\n")
