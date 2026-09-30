"""Publish through the real Git consumer without mutating shared site work."""
from pathlib import Path
import subprocess

import pytest

import publish_site_page as P
from test_publish_site_page import _git, _site


def _isolated(*args):
    # The old consumer is exercised on BASE; it must not get a fake success.
    return getattr(P, "publish_isolated", P.publish)(*args)


def _matrix(site):
    value = "prefix\n" + P.MATRIX_BEGIN + "\nold\n" + P.MATRIX_END + "\nsuffix\n"
    (site / "evaluation.html").write_text(value)
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-qm", "matrix")
    _git(site, "push", "-q", "origin", "main")
    return value


def test_dirty_source_and_staged_work_are_preserved_byte_for_byte(tmp_path):
    site, bare = _site(tmp_path)
    (site / "eda-forks.html").write_text("certified zero\n")
    (site / "manual.html").write_text("other session's staged work\n")
    _git(site, "add", "manual.html")
    (site / ".flow.html.stage").write_text("other session's temporary output\n")
    before_head = _git(site, "rev-parse", "HEAD")
    before_index = (site / ".git/index").read_bytes()
    before_files = {p.name: p.read_bytes() for p in site.iterdir() if p.is_file()}

    rc, record = _isolated(site, "eda-forks.html", "main", "dashboard: generated")

    assert rc == P.RC_OK, record
    assert _git(site, "rev-parse", "HEAD") == before_head
    assert (site / ".git/index").read_bytes() == before_index
    assert {p.name: p.read_bytes() for p in site.iterdir() if p.is_file()} == before_files
    remote = _git(bare, "rev-parse", "refs/heads/main")
    assert _git(bare, "diff-tree", "--no-commit-id", "--name-only", "-r", remote) == "eda-forks.html"
    assert _git(bare, "show", "main:manual.html") == "manual"


def test_origin_advancement_is_retained_without_updating_source(tmp_path):
    site, bare = _site(tmp_path)
    source_head = _git(site, "rev-parse", "HEAD")
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(bare), str(peer)], check=True)
    _git(peer, "config", "user.name", "Peer")
    _git(peer, "config", "user.email", "peer@example.com")
    (peer / "manual.html").write_text("new remote manual\n")
    _git(peer, "commit", "-qam", "peer change")
    _git(peer, "push", "-q", "origin", "main")
    remote_head = _git(bare, "rev-parse", "main")
    (site / "eda-forks.html").write_text("certified zero\n")

    rc, record = _isolated(site, "eda-forks.html", "main", "dashboard: generated")

    assert rc == P.RC_OK, record
    assert _git(site, "rev-parse", "HEAD") == source_head
    assert _git(bare, "rev-parse", "main^") == remote_head
    assert _git(bare, "show", "main:manual.html") == "new remote manual"


def test_only_matrix_region_moves_and_remote_surroundings_survive(tmp_path):
    site, bare = _site(tmp_path)
    original = _matrix(site)
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(bare), str(peer)], check=True)
    _git(peer, "config", "user.name", "Peer")
    _git(peer, "config", "user.email", "peer@example.com")
    (peer / "evaluation.html").write_text(original.replace("prefix", "remote prefix"))
    _git(peer, "commit", "-qam", "peer surroundings")
    _git(peer, "push", "-q", "origin", "main")
    (site / "evaluation.html").write_text(original.replace("old", "new"))
    (site / "eda-forks.html").write_text("certified zero\n")
    (site / "manual.html").write_text("unrelated local work\n")

    rc, record = _isolated(site, "eda-forks.html", "main", "dashboard: generated", ["evaluation.html"])

    assert rc == P.RC_OK, record
    actual = _git(bare, "show", "main:evaluation.html")
    assert actual == original.replace("old", "new").replace("prefix", "remote prefix").strip()
    assert (site / "manual.html").read_text() == "unrelated local work\n"


@pytest.mark.parametrize("edit", ["outside", "malformed"])
def test_matrix_rejections_leave_remote_unchanged(tmp_path, edit):
    site, bare = _site(tmp_path)
    value = _matrix(site)
    before = _git(bare, "rev-parse", "main")
    value = value.replace("prefix", "human edit") if edit == "outside" else value.replace(P.MATRIX_END, "")
    (site / "evaluation.html").write_text(value)
    (site / "eda-forks.html").write_text("certified zero\n")
    rc, _ = _isolated(site, "eda-forks.html", "main", "dashboard: generated", ["evaluation.html"])
    assert rc == P.RC_REFUSED
    assert _git(bare, "rev-parse", "main") == before


def test_generated_page_symlink_is_refused_before_external_file_write(tmp_path):
    site, bare = _site(tmp_path)
    external = tmp_path / "external.html"
    external.write_text("external unchanged\n")
    (site / "eda-forks.html").unlink()
    (site / "eda-forks.html").symlink_to(external)
    before = _git(bare, "rev-parse", "main")
    rc, _ = _isolated(site, "eda-forks.html", "main", "dashboard: generated")
    assert rc == P.RC_REFUSED
    assert external.read_text() == "external unchanged\n"
    assert _git(bare, "rev-parse", "main") == before


def test_source_change_during_clone_is_refused(tmp_path, monkeypatch):
    site, bare = _site(tmp_path)
    (site / "eda-forks.html").write_text("certified zero\n")
    before = _git(bare, "rev-parse", "main")
    real_run = P._run

    def raced(root, *args, **kwargs):
        result = real_run(root, *args, **kwargs)
        if args and args[0] == "clone":
            (site / "eda-forks.html").write_text("a newer generated page\n")
        return result

    monkeypatch.setattr(P, "_run", raced)
    rc, _ = _isolated(site, "eda-forks.html", "main", "dashboard: generated")
    assert rc == P.RC_REFUSED
    assert _git(bare, "rev-parse", "main") == before


def test_real_cli_selects_isolation_and_writes_readback_receipt(tmp_path):
    site, bare = _site(tmp_path)
    (site / "eda-forks.html").write_text("certified zero\n")
    (site / ".foreign.stage").write_text("foreign temporary file\n")
    receipt = tmp_path / "publication.json"
    try:
        rc = P.main(["--isolated", "--site-root", str(site), "--json", str(receipt)])
    except SystemExit as error:
        rc = error.code
    assert rc == 0
    import json
    data = json.loads(receipt.read_text())
    assert data["isolated"] is True
    assert data["remote_tip_after"] == _git(bare, "rev-parse", "main")
    assert (site / ".foreign.stage").read_text() == "foreign temporary file\n"
