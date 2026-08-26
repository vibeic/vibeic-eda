"""The 05:30 release boundary must not move a fork after the image is cut.

Measured 2026-08-26: ``daily_release`` published image 0.3.32 with OpenROAD
at 418da530, then the gatekeeper opened and landed vibeic/OpenROAD#8.  The
round had therefore created two commits past its own image pin.  Its certified
page was refused and the fallback page showed a live 22-commit gap even though
the round had converged to zero at its merge boundary.

``daily_merge.py`` is the release path's fork mutation authority.  The
gatekeeper that runs after ``daily_release`` is assessment/reporting only, and
the outer wrapper is the sole publisher of the certified page.
"""
from pathlib import Path


HERE = Path(__file__).resolve().parent
TICK = HERE / "run_tick.sh"
ROUND = HERE / "run_0530.sh"
GATEKEEPER = HERE / "gatekeeper.py"


def test_post_release_gatekeeper_is_forced_to_assessment_only():
    src = TICK.read_text(encoding="utf-8")
    release = src.index('python3 "${DIR}/daily_release.py"')
    gatekeeper = src.index("python3 gatekeeper.py")
    assert release < gatekeeper, "fixture no longer covers the post-release gatekeeper"

    between = src[release:gatekeeper]
    assert "export GK_MERGE_PR=0" in between, (
        "the post-release gatekeeper can still open/land a fork PR")
    assert "unset GK_RUN_HARNESS" in between, (
        "the post-release gatekeeper can still run the legacy promote harness")
    assert "export GK_DEFER_PUBLIC_PAGE=1" in between, (
        "the post-release gatekeeper can still overwrite the public page before "
        "the current-round certificate exists")


def test_outer_round_has_no_post_release_lander():
    src = ROUND.read_text(encoding="utf-8")
    tick = src.index('"${DIR}/run_tick.sh"')
    assert "land_merge_prs.py" not in src[tick:], (
        "run_0530 still mutates fork tips after run_tick has cut the image")


def test_gatekeeper_can_defer_its_uncertified_page_write():
    src = GATEKEEPER.read_text(encoding="utf-8")
    assert "GK_DEFER_PUBLIC_PAGE" in src
    assert "certified page" in src
