"""A fork whose ARG name is an ABBREVIATION of its repo name must still pair.

vibeic-eda#104 finding 2: the three asap7 forks are pinned by ASAP7SC_REF,
ASAP7PDK_REF and ASAP7KL_REF while their repos are asap7sc7p5t_28,
asap7_pdk_r1p7 and ASAP7_for_KLayout. No case/underscore folding turns one into
the other, so a checker that pairs by mangling the repo name reads all three as
`absent` and reports an outstanding release review that does not exist -- for
ever, since nothing about it decays.

That finding's fix taught `check_pins_current` to pair structurally and left
`check_release_pins_committed` with its own copy of the name-mangling. This
pins the second one.
"""
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import check_release_pins_committed as m           # noqa: E402

#: repo name -> the ARG that pins it. The point of the fixture is that no
#: string transform maps left to right.
ABBREVIATED = {
    "asap7sc7p5t_28": "ASAP7SC_REF",
    "asap7_pdk_r1p7": "ASAP7PDK_REF",
    "ASAP7_for_KLayout": "ASAP7KL_REF",
}


def _pins():
    try:
        return m.pins_at_rev(ROOT, "HEAD")
    except m.CouldNotTell as exc:
        pytest.skip(f"cannot read pins at HEAD here: {exc}")


def test_every_abbreviated_arg_pairs_to_its_repo():
    pins = _pins()
    missing = [r for r in ABBREVIATED if r not in pins]
    assert not missing, (
        "pinned in the Dockerfile but unpaired, so the check reports them as "
        f"absent at HEAD and demands a review that is already done: {missing}")


def test_the_paired_sha_is_the_one_the_dockerfile_states():
    """Pairing the right ARG matters more than pairing at all: an off-by-one
    would pair every repo to SOME sha and still be silently wrong."""
    text = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:Dockerfile"],
                          capture_output=True, text=True, timeout=60).stdout
    args = dict(m.REF_RE.findall(text))
    pins = _pins()
    for repo, arg in ABBREVIATED.items():
        want = args.get(arg.removesuffix("_REF"))
        assert want, f"{arg} is not in the Dockerfile any more; update this test"
        assert pins.get(repo) == want, (
            f"{repo} paired to {pins.get(repo)!r}, but {arg}={want!r}")


def test_no_arg_derived_alias_is_reported_as_a_fork():
    """The structural parser also returns an ARG-derived fallback name for the
    same pin (`asap7sc` beside `asap7sc7p5t_28`). Reporting both makes one fork
    look like two and puts a repo in the findings that RELEASED.json can never
    name -- which is what a first cut of the fix did.
    """
    pins = _pins()
    for alias in ("asap7sc", "asap7pdk", "asap7kl"):
        assert alias not in pins, (
            f"{alias} is an ARG-derived alias of a real repo, not a fork of "
            "ours; counting it invents a permanent outstanding review")
