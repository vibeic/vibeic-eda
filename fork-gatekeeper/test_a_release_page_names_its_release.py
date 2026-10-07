"""A release's page names the version that release just published.

Measured 2026-09-27: `daily_release` published 0.3.81 at 10:48 and rewrote
eda-forks.html at 10:49 by running `build_page` over the fork ledger that
`discover_forks` had written at 07:37, after 0.3.80. Every one of the 42 rows,
and the page headline read from `LEDGERS[0].image_version`, said 0.3.80 while
RELEASED.json and VERSION said 0.3.81. Nothing refreshed the ledger between the
version bump and the page, and nothing asked the page which version it named.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_page as PAGE                                      # noqa: E402
import daily_release as R                                      # noqa: E402
import gk_state                                                # noqa: E402

TOOLS = ("OpenROAD", "librelane", "yosys")


def _ledger(tmp_path: Path, monkeypatch, version: str) -> Path:
    state = tmp_path / "state"
    ledger = state / "ledger"
    ledger.mkdir(parents=True)
    for tool in TOOLS:
        (ledger / f"{tool}.json").write_text(json.dumps({
            "tool": tool, "generated_at": "2026-09-27T07:37:41+08:00",
            "image_version": version, "ahead": 0, "behind_commits": 0,
            "pin_kind": "pin", "integrated": True,
            "pinned_ref": "c" * 40, "pinned_ref_full": "c" * 40,
            "sync_lag": 0, "release_lag": 0,
            "ours_unshipped": 0, "ours_unshipped_substantive": 0,
        }), encoding="utf-8")
    monkeypatch.setattr(PAGE, "STATE", state)
    monkeypatch.setattr(PAGE, "LEDGER", ledger)
    monkeypatch.setattr(PAGE, "REPORTS", state / "reports")
    monkeypatch.setattr(PAGE, "RELEASE_STATE", state / "daily-release.json")
    monkeypatch.setattr(
        PAGE.inventory, "collect", lambda _image: {"a": [{"dir": "tool"}]})
    monkeypatch.setattr(PAGE, "_image_ref", lambda: "fixture/image:0")
    return ledger


def _embedded_versions(html: str) -> set:
    return set(__import__("re").findall(r'"image_version":\s*"([^"]+)"', html))


def test_a_ledger_from_the_previous_release_is_refused_and_nothing_written(
        tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, "0.3.80")
    out = tmp_path / "site" / "eda-forks.html"
    out.parent.mkdir()
    with pytest.raises(PAGE.PublicationEvidenceError,
                       match=r"does not describe image 0\.3\.81.*3 row"):
        PAGE.build(out, expect_image_version="0.3.81")
    assert not out.exists()


def test_the_cli_refuses_the_same_way(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, "0.3.80")
    out = tmp_path / "eda-forks.html"
    with pytest.raises(PAGE.PublicationEvidenceError):
        PAGE.main(["--out", str(out), "--expect-image-version", "0.3.81"])
    assert not out.exists()


def test_one_stale_or_absent_row_is_enough_to_refuse(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, monkeypatch, "0.3.81")
    row = json.loads((ledger / "yosys.json").read_text())
    row.pop("image_version")
    (ledger / "yosys.json").write_text(json.dumps(row))
    with pytest.raises(PAGE.PublicationEvidenceError, match="yosys=None"):
        PAGE.build(tmp_path / "p.html", expect_image_version="0.3.81")


def test_a_ledger_of_the_release_renders_that_release(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, "0.3.81")
    out = tmp_path / "eda-forks.html"
    PAGE.build(out, expect_image_version="0.3.81")
    assert _embedded_versions(out.read_text(encoding="utf-8")) == {"0.3.81"}


class _Proc:
    def __init__(self, rc: int, out: str = ""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _public_output(tmp_path, monkeypatch, spelling):
    """Use real resolved-path classification without touching the actual site."""
    public = tmp_path / "site" / "eda-forks.html"
    public.parent.mkdir()
    public.write_text("LAST COMPLETE PAGE", encoding="utf-8")
    monkeypatch.setattr(gk_state, "PRODUCTION_PAGE", str(public))
    # BASE's early publisher used this default; keep the red control local.
    monkeypatch.setattr(R, "_PRODUCTION_PAGE", str(public), raising=False)
    monkeypatch.setenv("GK_PRODUCTION_WRITER", "1")
    if spelling == "directory-symlink":
        alias = tmp_path / "site-alias"
        alias.symlink_to(public.parent, target_is_directory=True)
        out = alias / public.name
    elif spelling == "file-symlink":
        out = tmp_path / "page-alias.html"
        out.symlink_to(public)
    elif spelling == "normalized":
        out = public.parent / ".." / "site" / public.name
    else:
        out = public
    return public, out


@pytest.mark.parametrize("spelling", [
    "default", "empty-default", "canonical", "directory-symlink",
    "file-symlink", "normalized",
])
def test_release_page_waits_for_complete_round_before_any_child_or_write(
        tmp_path, monkeypatch, spelling):
    _ledger(tmp_path, monkeypatch, "0.3.81")
    public, out = _public_output(tmp_path, monkeypatch, spelling)
    calls = []

    def run(cmd, **kw):
        calls.append(Path(cmd[1]).name)
        if cmd[1].endswith("build_page.py"):
            # Run the real consumer: BASE actually replaces this last-good
            # page before LL/current-round authorization has even happened.
            return _Proc(PAGE.main(cmd[2:]) or 0)
        return _Proc(0, "ledger ready")

    kwargs = {} if spelling == "default" else {"out": str(out)}
    if spelling == "empty-default":
        kwargs["out"] = ""
    ok, note = R.publish_release_page("0.3.81", run=run, **kwargs)

    assert public.read_text(encoding="utf-8") == "LAST COMPLETE PAGE"
    assert calls == [], "release ran an uncertified discovery/page child"
    if spelling == "file-symlink":
        assert out.is_symlink()
    assert ok is None, "deferral must not claim page success or failure"
    assert "deferred" in note.lower() and "current-round" in note


@pytest.mark.parametrize("spelling", [
    "canonical", "directory-symlink", "file-symlink", "normalized",
])
def test_production_output_without_certificate_is_refused_before_inventory(
        tmp_path, monkeypatch, spelling):
    _ledger(tmp_path, monkeypatch, "0.3.81")
    public, out = _public_output(tmp_path, monkeypatch, spelling)
    calls = []
    monkeypatch.setattr(PAGE.inventory, "collect",
                        lambda image: calls.append(image) or {"a": []})
    refusal = None
    try:
        PAGE.main(["--out", str(out), "--expect-image-version", "0.3.81"])
    except PAGE.PublicationEvidenceError as exc:
        refusal = exc

    assert public.read_text(encoding="utf-8") == "LAST COMPLETE PAGE"
    assert calls == [], "production refusal occurred after inventory work"
    assert isinstance(refusal, PAGE.PublicationEvidenceError)
    assert "current-round evidence" in str(refusal)
    if spelling == "file-symlink":
        assert out.is_symlink()


def test_the_release_refreshes_the_ledger_before_it_renders_the_page():
    calls = []

    def run(cmd, **kw):
        calls.append((Path(cmd[1]).name, cmd[2:], kw.get("env", {})))
        return _Proc(0, "ok")

    ok, _note = R.publish_release_page("0.3.81", run=run, out="/x/p.html")
    assert ok
    assert [c[0] for c in calls] == ["discover_forks.py", "build_page.py"]
    page_args = calls[1][1]
    assert page_args[page_args.index("--expect-image-version") + 1] == "0.3.81"
    assert page_args[page_args.index("--out") + 1] == "/x/p.html"
    assert all(c[2].get("GK_PRODUCTION_WRITER") == "1" for c in calls)


def test_a_refused_page_is_a_failed_page_with_both_reasons():
    def run(cmd, **kw):
        if cmd[1].endswith("discover_forks.py"):
            return _Proc(1, "GitHub VERSION unreadable")
        return _Proc(2, "build_page: REFUSED — ledger does not describe image")

    ok, note = R.publish_release_page("0.3.81", run=run, out="/x/p.html")
    assert not ok
    assert "ledger refresh rc=1" in note and "REFUSED" in note


def test_a_runner_exception_does_not_escape_the_release():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    ok, note = R.publish_release_page("0.3.81", run=run, out="/x/p.html")
    assert not ok and "TimeoutExpired" in note


def test_the_release_path_uses_the_function_that_names_the_version():
    src = Path(R.__file__).read_text(encoding="utf-8")
    # the publish branch calls it with the version it just pushed
    assert src.count("publish_release_page(new)") == 1
    # the only build_page invocation left in daily_release is the one that
    # passes the version it is publishing
    assert src.count('"build_page.py"') == 1


def test_a_stubbed_module_subprocess_is_the_one_it_runs(monkeypatch):
    """The release tests stub `daily_release.subprocess.run` module-wide. The
    page step must go through that stub, or a test run executes the real
    discover_forks/build_page as the production writer (it did, 2026-09-27
    11:52, rewriting the production ledger from a scratch checkout)."""
    seen = []

    def stub(cmd, **kw):
        seen.append(Path(cmd[1]).name)
        return _Proc(0, "stubbed")

    monkeypatch.setattr(R.subprocess, "run", stub)
    ok, _ = R.publish_release_page("0.3.81", out="/x/p.html")
    assert ok and seen == ["discover_forks.py", "build_page.py"]
