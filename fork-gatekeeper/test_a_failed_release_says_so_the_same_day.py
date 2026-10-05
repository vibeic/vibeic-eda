"""A release that dies mid-build was invisible until the page went stale.

2026-08-22: the round merged 46 upstream klayout commits, the image build died
on a version assertion pinned to the PREVIOUS klayout, `build_page` was skipped —
and the round finished normally with nothing raised. `check_publish_silence` was
rc 0 because the page had published the day before and the streak was under its
threshold. The only alarm in the pipeline measures the PAGE going stale, which is
a consequence, and measures it in DAYS. A build failing identically every round
could sit most of a week before anything said a word.

The value is in what it stays QUIET about, so that is most of this file: an
alarm that fires on healthy rounds is one nobody reads.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_release_failed as R  # noqa: E402
import fork_github_convergence as CONV  # noqa: E402

ROUND_AT = "2026-08-22T05:30:01+08:00"


def _run(*args, dryrun=True):
    env = {"PATH": "/usr/bin:/bin", "GK_PR_DRYRUN": "1" if dryrun else "0",
           "HOME": str(Path.home())}
    return subprocess.run(
        [sys.executable, str(HERE / "check_release_failed.py"),
         "--round-at", ROUND_AT, *args],
        capture_output=True, text=True, timeout=120, env=env)


# --- when it must stay quiet -------------------------------------------------

def test_a_successful_release_raises_nothing():
    r = _run("--tick-exit", "0")
    assert r.returncode == R.RC_OK
    assert "succeeded" in r.stdout


@pytest.mark.parametrize("state", [
    "not_checked_pytest_red", "not_checked_ai_norecord",
    "not_checked_stale_round_code",
    "not_checked_preflight_discovery_failed",
    "not_checked_preflight_discovery_norecord",
])
def test_a_round_that_never_reached_a_release_raises_nothing(state):
    """NOT CHECKED IS NOT A FAILURE. Whatever stopped the round earlier is its
    own gate's business and has already said so; a second alarm for it is noise
    that trains the reader to ignore the first."""
    r = _run("--tick-exit", "2", "--tick-state", state)
    assert r.returncode == R.RC_OK
    assert "no release was attempted" in r.stdout


# --- when it must fire -------------------------------------------------------

def test_a_failed_release_is_raised_on_the_day_it_failed():
    """THE CASE THIS EXISTS FOR — today's round, verbatim."""
    r = _run("--tick-exit", "5")
    assert r.returncode == R.RC_ESCALATED
    assert "release failed on the 2026-08-22 round" in r.stdout
    assert "run_tick exit 5" in r.stdout


def test_an_unreadable_tick_exit_is_norecord_not_silence():
    """A code that cannot be read is not evidence of success. Refusing loudly is
    the only other honest option."""
    r = _run("--tick-exit", "")
    assert r.returncode == R.RC_NORECORD
    assert "NORECORD" in r.stdout


# --- the part that would make it counterproductive ---------------------------

def test_the_alarm_is_not_counted_as_open_work():
    """AN ALARM IS NOT A DEBT. `fork_github_convergence` counts what the fleet
    OWES; a PR the round opened to REPORT a problem is not owed work. Miss this
    and the alarm blocks the very round it is reporting on — the failure #138
    fixed for publish-silence, which this must not reintroduce."""
    item = {"repository": CONV.VIBEIC_EDA_REPOSITORY,
            "head_ref": f"{R.ALARM_KIND}-2026-08-22"}
    assert CONV._is_alarm(item) is True


def test_the_other_alarm_is_still_excluded():
    item = {"repository": CONV.VIBEIC_EDA_REPOSITORY,
            "head_ref": "publish-silence-2026-08-22"}
    assert CONV._is_alarm(item) is True


def test_ordinary_work_is_still_counted():
    """The exclusion must not widen into 'nothing on this repo counts'."""
    item = {"repository": CONV.VIBEIC_EDA_REPOSITORY,
            "head_ref": "fix/some-real-work"}
    assert CONV._is_alarm(item) is False


def test_the_alarm_kind_is_an_excluded_prefix():
    """The branch name and the exclusion list must agree BY CONSTRUCTION. They
    live in different files, and a rename on one side alone is how an alarm
    silently becomes open work."""
    assert any(p.rstrip("-") == R.ALARM_KIND for p in CONV.ALARM_BRANCH_PREFIXES), (
        f"{R.ALARM_KIND!r} is not in {CONV.ALARM_BRANCH_PREFIXES}")


def test_one_pr_per_failing_day():
    """A round that fails four times in a day must not open four PRs. The date
    is in the branch name and an existing PR means already-raised."""
    src = (HERE / "check_publish_silence.py").read_text(encoding="utf-8")
    assert 'branch = f"{kind}-{date}"' in src
    assert '"--state", "all"' in src
    assert "already raised for" in src
