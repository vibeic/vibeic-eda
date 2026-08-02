"""A branch name must come from git, never from the sentence beside the pin.

Every case here was found on origin/main by running the OLD pattern over all 17
Dockerfiles; none is invented. The old pattern was
`ARG\\s+(\\w+_REF)\\s*=\\s*\\S+\\s*#[^\\n]*branch\\s+(\\S+)` — `[^\\n]*` is greedy, so it
consumed the comment and backtracked to the LAST "branch X" on the line.
"""
import re
import discover_forks as df


def test_the_last_mention_is_the_retired_branch_not_the_current_one():
    """Trilinos: the comment documents a pin CHANGE, so the last mention is the old one.

    This is the case that mattered. Everything downstream measured the retired branch, so
    the daily round reported 1257 commits behind and handed a human 31 file conflicts every
    morning — while the branch actually shipped is 0 behind upstream.
    """
    c = (" branch vibeic/xyce-trilinos-17.2-epetra-restored -- Trilinos 17.2.0-dev "
         "(upstream master 763aa751877) with the SEVEN packages Xyce requires restored. "
         "Stock 17.x does NOT build Xyce; only this branch does. Previous pin: cf47480689f "
         "(branch vibeic/xyce-trilinos-16.2.1), which stays untouched as the fallback.")
    assert df._branch_from_comment(c) == "vibeic/xyce-trilinos-17.2-epetra-restored"


def test_an_ordinary_word_after_branch_is_not_a_branch():
    """cocotb: the comment says the branch it used to track was DELETED.

    The old pattern read that sentence and stored a branch called `this`.
    """
    c = (" branch master -- the feature branch this used to track was deleted, leaving the "
         "pin reachable from NO branch, so git could garbage-collect it")
    assert df._branch_from_comment(c) == "master"


def test_trailing_punctuation_is_not_part_of_the_name():
    """sby: a branch name followed by a colon and a list."""
    c = " branch vibeic/integration: V23/V24/V26/V30 (main) + V42/V27 (w3), package layout"
    assert df._branch_from_comment(c) == "vibeic/integration"


def test_prose_that_merely_mentions_branches_yields_nothing():
    assert df._branch_from_comment(" no pin here, just words about a branch really") is None
    assert df._branch_from_comment(" only this branch does.") is None


def test_the_old_greedy_pattern_would_fail_every_case_above():
    """The control. If this ever passes, the regression has been reintroduced."""
    old = re.compile(r"#[^\n]*branch\s+(\S+)")
    for comment, correct in [
        (" branch vibeic/a -- x. Previous pin: deadbeef (branch vibeic/b), the fallback.", "vibeic/a"),
        (" branch master -- the feature branch this used to track was deleted", "master"),
    ]:
        got = old.search("#" + comment).group(1)
        assert got != correct, f"old pattern unexpectedly correct on {comment!r}"
        assert df._branch_from_comment(comment) == correct
