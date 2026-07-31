"""The absence-claims gate verified a superseded image.

`check_fork_presence_claims.py` carried:

    DEFAULT_IMAGE = "ghcr.io/vibeic/vibeic-eda:0.2.46"

while this repo's VERSION said 0.2.47. Every default run therefore checked the
ledger's absence claims against a PUBLISHED-BUT-SUPERSEDED image and passed —
saying nothing about what we ship today. A tool that entered the image in 0.2.47
while its ledger entry still claimed absence is exactly the contradiction this
gate exists to find, and the gate would not have looked at it.

Same shape as the stale image anchor in vibe-ic v1.8.86: a constant that was
right when written and had no way to notice the world moving past it.

The fallback is asserted too. A missing VERSION must warn and continue, not
raise: a gate that dies on a missing file is a gate that gets wrapped in
`|| true` by the next person who hits it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
_PROG = _REPO / "tools" / "check_fork_presence_claims.py"


def _load(module_name="check_fork_presence_claims"):
    spec = importlib.util.spec_from_file_location(module_name, _PROG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_program_exists():
    """Anchors every other assertion here; a moved file must fail loudly."""
    assert _PROG.is_file(), _PROG


def test_default_image_matches_this_repos_version():
    version = (_REPO / "VERSION").read_text(encoding="utf-8").strip()
    mod = _load()
    assert mod.DEFAULT_IMAGE.endswith(f":{version}"), (
        f"DEFAULT_IMAGE is {mod.DEFAULT_IMAGE} but VERSION says {version}; the "
        f"gate would verify absence claims against an image we no longer ship")


def test_the_tag_is_not_hard_coded(monkeypatch, tmp_path):
    """Reading VERSION, not coincidentally equal to it.

    Without this, a hard-coded constant that happens to match today's VERSION
    satisfies the test above and drifts again the next time VERSION moves.
    """
    src = _PROG.read_text(encoding="utf-8")
    assert 'DEFAULT_IMAGE = "ghcr.io/vibeic/vibeic-eda:' not in src, (
        "DEFAULT_IMAGE is assigned a literal tag again")
    assert "VERSION" in src, "nothing in the program reads VERSION"


def test_a_missing_version_warns_and_falls_back(tmp_path, capsys, monkeypatch):
    """The gate must not die on a missing VERSION.

    Exercised through the program's own resolver rather than by deleting the
    real file, so the repo is never mutated by a test.
    """
    mod = _load("check_fork_presence_claims_probe")
    monkeypatch.setattr(
        mod.pathlib.Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone")))
    tag = mod._default_image()
    captured = capsys.readouterr()
    assert tag.startswith("ghcr.io/vibeic/vibeic-eda:"), tag
    assert "WARNING" in captured.err, captured.err
    assert "may not be what this repo ships" in captured.err, captured.err
