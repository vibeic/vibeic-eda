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
    monkeypatch.setattr(df, "_ls_remote_head", _fake_heads({}))
    cmp, source = df._unpinned_head_compare("ciel", "fossi-foundation/ciel", "main", "main")
    assert cmp is None and source is None


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
                  'if cmp.get("_err") and ref:'):
        assert gated in src, f"expected still-ref-gated route missing: {gated}"
