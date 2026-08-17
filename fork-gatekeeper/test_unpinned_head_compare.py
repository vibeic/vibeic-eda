"""An unpinned mirror is not an unanswerable one — vibeic-eda#54.

Four forks (ciel, open_pdks, sv2v, IHP-Open-PDK) reported `behind_commits: null`
and `fork_point_status: undetermined`, because every compare route asks about a
PINNED ref and the image pins none of them. The page rendered that as
"could not be measured".

It was measurable. All four sit exactly on the upstream head, which two
`ls-remote` calls establish. The SHAs below are the real ones, read from the
remotes on 2026-08-02.
"""
import pytest
import discover_forks as df
import subprocess

# measured 2026-08-02 — our mirror and upstream agreed to the commit
REAL = {
    "ciel":         ("fossi-foundation/ciel",      "main",   "714d1bbb626d"),
    "open_pdks":    ("fossi-foundation/open-pdks", "main",   "c0eb16d5d3d7"),
    "sv2v":         ("zachjs/sv2v",                "master", "6662fa5da71f"),
    "IHP-Open-PDK": ("IHP-GmbH/IHP-Open-PDK",      "main",   "22f2a25f1734"),
}


def _fake_heads(mapping):
    def _f(url, branch):
        return mapping.get((url, branch))
    return _f


@pytest.mark.parametrize("tool", sorted(REAL))
def test_a_mirror_on_the_upstream_head_is_zero_not_unknown(tool, monkeypatch):
    up_full, up_branch, sha = REAL[tool]
    monkeypatch.setattr(df, "_clone_for", lambda *a: None)
    monkeypatch.setattr(df, "_local_compare", lambda *a: None)
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({
        (f"https://github.com/vibeic/{tool}.git", up_branch): sha,
        (f"https://github.com/{up_full}.git", up_branch): sha,
    }))
    cmp, source = df._unpinned_head_compare(tool, up_full, up_branch, up_branch)
    assert cmp is not None and "_err" not in cmp, cmp
    assert cmp["behind_by"] == 0 and cmp["ahead_by"] == 0
    assert source == "unpinned-head-identical"


def test_a_divergent_head_is_never_rendered_as_zero(monkeypatch):
    """The one that matters. A gap we cannot SIZE must not read as no gap.

    `ls-remote` proves only (in)equality. Turning "these two SHAs differ" into a
    count would have to guess, and a guess here lands on 'barely behind' — the
    reassuring direction, and the one a reader believes.
    """
    monkeypatch.setattr(df, "_clone_for", lambda *a: None)
    monkeypatch.setattr(df, "_local_compare", lambda *a: None)
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({
        ("https://github.com/vibeic/open_pdks.git", "main"): "c0eb16d5d3d7",
        ("https://github.com/fossi-foundation/open-pdks.git", "main"): "deadbeef1234",
    }))
    cmp, source = df._unpinned_head_compare(
        "open_pdks", "fossi-foundation/open-pdks", "main", "main")
    assert source is None
    assert "_err" in cmp
    assert "behind_by" not in cmp and "ahead_by" not in cmp
    assert "needs a clone" in cmp["_err"]


def test_an_unreachable_remote_adds_nothing_and_does_not_claim_zero(monkeypatch):
    """No answer is not the answer zero — the caller's own refusal should stand."""
    monkeypatch.setattr(df, "_clone_for", lambda *a: None)
    monkeypatch.setattr(df, "_local_compare", lambda *a: None)
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({}))
    cmp, source = df._unpinned_head_compare("ciel", "fossi-foundation/ciel", "main", "main")
    assert cmp is None and source is None


def test_a_divergent_unpinned_clone_measures_the_live_watch_only_shape(
        tmp_path, monkeypatch):
    """icsprout55-pdk is three commits ahead, not ``ahead=null``.

    The exact live shape is a divergent, unpinned mirror with a local clone.  A
    remote-SHA equality probe can only say "different"; the clone owns the
    graph needed to measure the three carried commits.  It must run before the
    equality-only fallback so a future edit cannot restore the false zero.
    """
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", "-q", "-b", "main", str(remote)],
                   check=True)

    def git(repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "user.name", "test")
    (remote / "base").write_text("base")
    git(remote, "add", "base")
    git(remote, "commit", "-qm", "upstream base")
    upstream_sha = git(remote, "rev-parse", "HEAD")

    clone = tmp_path / "icsprout55-pdk"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    for index in range(3):
        (remote / f"ours-{index}").write_text(str(index))
        git(remote, "add", f"ours-{index}")
        git(remote, "commit", "-qm", f"ours {index}")
    remote_sha = git(remote, "rev-parse", "HEAD")
    # The mutable local name is stale AND the new remote object is absent.
    # Passing `main` would report ahead=0.  The route must fetch the object
    # without moving main, then compare the immutable live fork SHA.
    assert git(clone, "rev-parse", "main") == upstream_sha
    missing = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{remote_sha}^{{commit}}"])
    assert missing.returncode != 0

    monkeypatch.setattr(df, "_clone_for", lambda tool: clone)
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({
        ("https://github.com/vibeic/icsprout55-pdk.git", "main"): remote_sha,
        ("https://github.com/openecos-projects/icsprout55-pdk.git", "main"):
            upstream_sha,
    }))
    real_git = df._git

    def route_test_fetch(repo, *args, **kwargs):
        if (args and args[0] == "fetch"
                and "https://github.com/vibeic/icsprout55-pdk.git" in args):
            args = tuple(str(remote) if value ==
                         "https://github.com/vibeic/icsprout55-pdk.git"
                         else value for value in args)
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(df, "_git", route_test_fetch)

    cmp, source = df._unpinned_head_compare(
        "icsprout55-pdk", "openecos-projects/icsprout55-pdk", "main", "main")

    assert cmp["ahead_by"] == 3 and cmp["behind_by"] == 0
    assert source == "local-clone"
    assert git(clone, "rev-parse", "main") == upstream_sha
    assert git(clone, "cat-file", "-t", remote_sha) == "commit"


def test_a_failed_fork_head_fetch_stays_unknown(monkeypatch):
    """Fetch command success is not assumed, and failure never becomes zero."""
    ours, theirs = "b" * 40, "a" * 40
    monkeypatch.setattr(df, "_clone_for", lambda tool: "/clone")
    monkeypatch.setattr(df, "_peel", lambda *a: {})
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({
        ("https://github.com/vibeic/icsprout55-pdk.git", "main"): ours,
        ("https://github.com/openecos-projects/icsprout55-pdk.git", "main"):
            theirs,
    }))
    fetches = []

    class Failed:
        ok = False

    def fail_fetch(repo, *args, **kwargs):
        fetches.append(args)
        return Failed()

    monkeypatch.setattr(df, "_git", fail_fetch)
    monkeypatch.setattr(
        df, "_local_compare",
        lambda *a: pytest.fail("compare ran without the current fork object"))

    cmp, source = df._unpinned_head_compare(
        "icsprout55-pdk", "openecos-projects/icsprout55-pdk", "main", "main")

    assert source is None and "_err" in cmp
    assert "ahead_by" not in cmp and "behind_by" not in cmp
    assert len(fetches) == 1
    assert "https://github.com/vibeic/icsprout55-pdk.git" in fetches[0]


def test_the_route_is_what_answers_these_four():
    """Control: without this route the four are undetermined by construction.

    Every other route is gated on `ref`, and these four have none — so a test that
    passes only because some other path answered would be measuring nothing. This
    asserts the gate that used to send them to the refusal.
    """
    src = open(df.__file__, encoding="utf-8").read()
    assert 'if cmp.get("_err") and not ref:' in src, \
        "the unpinned route must be the one that fires when there is no pin"
    for gated in ('_local_compare(tool, up_full, up_branch, ref) if ref else None',
                  'local = _local_compare(tool, up_full, up_branch, ours)',
                  'if cmp.get("_err") and ref:'):
        assert gated in src, f"expected still-ref-gated route missing: {gated}"
