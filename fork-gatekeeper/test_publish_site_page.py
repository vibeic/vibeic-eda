from __future__ import annotations

import subprocess
from pathlib import Path

import publish_site_page as P


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _site(tmp_path: Path):
    site = tmp_path / "site"
    site.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(site)], check=True)
    _git(site, "config", "user.name", "Test Author")
    _git(site, "config", "user.email", "test@example.com")
    (site / "eda-forks.html").write_text("old\n", encoding="utf-8")
    (site / "manual.html").write_text("manual\n", encoding="utf-8")
    _git(site, "add", "eda-forks.html", "manual.html")
    _git(site, "commit", "-q", "-m", "initial site")
    bare = tmp_path / "site.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(site, "remote", "add", "origin", str(bare))
    _git(site, "push", "-q", "-u", "origin", "main")
    return site, bare


def test_certified_page_is_the_only_file_committed_and_pushed(tmp_path):
    site, bare = _site(tmp_path)
    (site / "eda-forks.html").write_text("certified zero\n", encoding="utf-8")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state")

    assert rc == P.RC_OK and record["status"] == "published"
    remote = _git(bare, "rev-parse", "refs/heads/main")
    assert remote == record["commit"] == record["remote_tip_after"]
    assert _git(bare, "show", f"{remote}:eda-forks.html") == "certified zero"
    assert _git(bare, "show", f"{remote}:manual.html") == "manual"
    assert _git(site, "status", "--porcelain") == ""


def test_unrelated_site_change_is_refused_without_a_commit(tmp_path):
    site, bare = _site(tmp_path)
    old_remote = _git(bare, "rev-parse", "refs/heads/main")
    (site / "eda-forks.html").write_text("certified zero\n", encoding="utf-8")
    (site / "manual.html").write_text("somebody else's work\n", encoding="utf-8")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state")

    assert rc == P.RC_REFUSED and record["status"] == "refused"
    assert "manual.html" in record["detail"]
    assert _git(bare, "rev-parse", "refs/heads/main") == old_remote
    assert _git(site, "rev-parse", "HEAD") == old_remote


def test_generated_page_noop_still_verifies_remote(tmp_path):
    site, bare = _site(tmp_path)
    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state")
    assert rc == P.RC_OK and record["status"] == "published"
    assert record["commit"] == record["remote_tip_before"]
    assert record["remote_tip_after"] == record["commit"]


def test_hourly_matrix_region_can_publish_with_the_certified_page(tmp_path):
    site, bare = _site(tmp_path)
    matrix = ("prefix\n<!-- IC_PDK_MATRIX:BEGIN -->\nold matrix\n"
              "<!-- IC_PDK_MATRIX:END -->\nsuffix\n")
    (site / "evaluation.html").write_text(matrix, encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "add generated matrix page")
    _git(site, "push", "-q", "origin", "main")
    (site / "eda-forks.html").write_text("certified zero\n", encoding="utf-8")
    (site / "evaluation.html").write_text(
        matrix.replace("old matrix", "new matrix"), encoding="utf-8")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state",
        ["evaluation.html"])

    assert rc == P.RC_OK and record["status"] == "published"
    remote = _git(bare, "rev-parse", "refs/heads/main")
    assert "new matrix" in _git(bare, "show", f"{remote}:evaluation.html")
    assert _git(site, "status", "--porcelain") == ""


def test_matrix_change_outside_generator_markers_is_refused(tmp_path):
    site, bare = _site(tmp_path)
    matrix = ("prefix\n<!-- IC_PDK_MATRIX:BEGIN -->\nmatrix\n"
              "<!-- IC_PDK_MATRIX:END -->\nsuffix\n")
    (site / "evaluation.html").write_text(matrix, encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "add generated matrix page")
    _git(site, "push", "-q", "origin", "main")
    old_remote = _git(bare, "rev-parse", "refs/heads/main")
    (site / "eda-forks.html").write_text("certified zero\n", encoding="utf-8")
    (site / "evaluation.html").write_text(
        matrix.replace("prefix", "human edit"), encoding="utf-8")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state",
        ["evaluation.html"])

    assert rc == P.RC_REFUSED and record["status"] == "refused"
    assert "outside" in record["detail"]
    assert _git(bare, "rev-parse", "refs/heads/main") == old_remote
    assert _git(site, "diff", "--cached", "--name-only") == ""


def test_unpushed_generated_matrix_commit_can_be_recovered(tmp_path):
    site, bare = _site(tmp_path)
    matrix = ("prefix\n<!-- IC_PDK_MATRIX:BEGIN -->\nold matrix\n"
              "<!-- IC_PDK_MATRIX:END -->\nsuffix\n")
    (site / "evaluation.html").write_text(matrix, encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "add generated matrix page")
    _git(site, "push", "-q", "origin", "main")
    (site / "evaluation.html").write_text(
        matrix.replace("old matrix", "new matrix"), encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "hourly generated matrix")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state",
        ["evaluation.html"])

    assert rc == P.RC_OK and record["status"] == "published"
    remote = _git(bare, "rev-parse", "refs/heads/main")
    assert remote == _git(site, "rev-parse", "HEAD")
    assert "new matrix" in _git(bare, "show", f"{remote}:evaluation.html")


def test_unpushed_matrix_commit_with_manual_edit_is_not_swept(tmp_path):
    site, bare = _site(tmp_path)
    matrix = ("prefix\n<!-- IC_PDK_MATRIX:BEGIN -->\nmatrix\n"
              "<!-- IC_PDK_MATRIX:END -->\nsuffix\n")
    (site / "evaluation.html").write_text(matrix, encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "add generated matrix page")
    _git(site, "push", "-q", "origin", "main")
    old_remote = _git(bare, "rev-parse", "refs/heads/main")
    (site / "evaluation.html").write_text(
        matrix.replace("prefix", "manual edit"), encoding="utf-8")
    _git(site, "add", "evaluation.html")
    _git(site, "commit", "-q", "-m", "manual matrix-page edit")

    rc, record = P.publish(
        site, "eda-forks.html", "main", "dashboard: publish certified state",
        ["evaluation.html"])

    assert rc == P.RC_REFUSED and record["status"] == "refused"
    assert "unpushed site commit" in record["detail"]
    assert _git(bare, "rev-parse", "refs/heads/main") == old_remote
