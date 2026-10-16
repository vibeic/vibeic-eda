"""The branch-at-pin lookup has three outcomes, not an Optional[str].

An API failure is absence of evidence.  It must never be published as evidence
that no branch carries the pin, and it must survive into the retained JSON even
when another fork in the same round can be merged normally.
"""
import json
import pathlib
import subprocess
import sys
from urllib.parse import quote

import pytest


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_merge as M  # noqa: E402


PIN = "a" * 40
ORIGIN_TIP = "b" * 40


@pytest.fixture(autouse=True)
def no_network_retry_sleep(monkeypatch):
    # Both the git probe and shared GitHub GET helper use the time module.
    # Tests still observe every request without waiting through real backoff.
    monkeypatch.setattr(M.time, "sleep", lambda _seconds: None)


def _declared(monkeypatch, branch="master"):
    monkeypatch.setattr(M, "_declared_branch", lambda repo: branch)


def test_a_transport_failure_is_unknown_not_no_branch(monkeypatch):
    _declared(monkeypatch)
    monkeypatch.setattr(
        M, "_sh",
        lambda *a, **k: (127, "", "TimeoutExpired: GitHub did not answer"),
    )
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None, got
    assert "rc=127" in got.detail, got


def test_only_a_complete_listing_can_authoritatively_say_absent(monkeypatch):
    _declared(monkeypatch)
    def answered(cmd, *a, **k):
        if "/branches?" in " ".join(cmd):
            return 0, f"master {'b' * 40}\n", ""
        return 0, json.dumps({"status": "diverged"}), ""

    monkeypatch.setattr(M, "_sh", answered)
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_ABSENT, got
    assert "does not contain pin" in got.detail, got


def test_malformed_success_is_unknown_not_authoritative_absence(monkeypatch):
    _declared(monkeypatch)
    monkeypatch.setattr(M, "_sh", lambda *a, **k: (0, "partial-page", ""))
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None, got


def test_even_an_exact_tip_is_verified_by_one_declared_branch_compare(monkeypatch):
    _declared(monkeypatch, "vibeic/integration")
    calls = []

    def exact(cmd, *a, **k):
        calls.append(cmd)
        if "/branches?" in " ".join(cmd):
            return 0, f"vibeic/integration {PIN}\n", ""
        return 0, json.dumps({"status": "identical"}), ""

    monkeypatch.setattr(M, "_sh", exact)
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_FOUND
    assert got.branch == "vibeic/integration"
    assert len(calls) == 2
    assert sum("/compare/" in " ".join(call) for call in calls) == 1


def test_a_pin_behind_master_resolves_to_the_containing_branch(monkeypatch):
    """The state between daily merge and daily release must not deadlock.

    The branch tip has advanced to B while the image still pins A. A is no
    longer an exact tip, but compare A...master proves master contains it.
    """
    _declared(monkeypatch)
    tip = "b" * 40
    calls = []

    def answered(cmd, *a, **k):
        calls.append(cmd)
        if "/branches?" in " ".join(cmd):
            return 0, f"master {tip}\n", ""
        assert f"compare/{PIN}...master" in " ".join(cmd)
        return 0, json.dumps({"status": "ahead", "total_commits": 1}), ""

    monkeypatch.setattr(M, "_sh", answered)
    got = M.branch_at_pin("yosys", PIN)

    assert got.status == M.BRANCH_FOUND, got
    assert got.branch == "master"
    assert "contains pin" in got.detail
    assert len(calls) == 2


def test_compare_failure_without_a_hit_is_unknown(monkeypatch):
    _declared(monkeypatch)
    def unanswered(cmd, *a, **k):
        if "/branches?" in " ".join(cmd):
            return 0, f"master {'b' * 40}\n", ""
        return 124, "", "request deadline expired"

    monkeypatch.setattr(M, "_sh", unanswered)
    got = M.branch_at_pin("yosys", PIN)

    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None
    assert got.rc == 124
    assert "compare pin...master returned rc=124" in got.detail


def test_malformed_compare_without_a_hit_is_unknown(monkeypatch):
    _declared(monkeypatch)
    def malformed(cmd, *a, **k):
        if "/branches?" in " ".join(cmd):
            return 0, f"master {'b' * 40}\n", ""
        return 0, "not-json", ""

    monkeypatch.setattr(M, "_sh", malformed)
    got = M.branch_at_pin("yosys", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.rc == 0


def test_two_descendants_choose_declared_master_without_probing_the_other(
        monkeypatch):
    """Containment is not identity; only the declared line may be advanced."""
    _declared(monkeypatch)
    old_integration = "vibeic/old-integration"
    calls = []

    def mixed(cmd, *a, **k):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "/branches?" in joined:
            return 0, (f"{old_integration} {'b' * 40}\n"
                       f"master {'c' * 40}\n"), ""
        if "old-integration" in joined:
            raise AssertionError("the non-declared descendant was compared")
        if "...master" in joined:
            return 0, json.dumps({"status": "ahead"}), ""
        raise AssertionError(cmd)

    monkeypatch.setattr(M, "_sh", mixed)
    got = M.branch_at_pin("yosys", PIN)

    assert got.status == M.BRANCH_FOUND, got
    assert got.branch == "master"
    assert len(calls) == 2
    assert sum("/compare/" in " ".join(call) for call in calls) == 1


def test_795_branch_listing_still_performs_exactly_one_compare(monkeypatch):
    _declared(monkeypatch)
    calls = []
    rows = [f"topic-{i} {i:040x}" for i in range(794)]
    rows.append(f"master {'f' * 40}")

    def answered(cmd, *a, **k):
        calls.append(cmd)
        if "/branches?" in " ".join(cmd):
            return 0, "\n".join(rows) + "\n", ""
        return 0, json.dumps({"status": "ahead"}), ""

    monkeypatch.setattr(M, "_sh", answered)
    got = M.branch_at_pin("large", PIN)
    assert got.status == M.BRANCH_FOUND, got
    assert got.branch == "master"
    assert len(calls) == 2
    assert sum("/compare/" in " ".join(call) for call in calls) == 1


def test_missing_declaration_allows_only_a_unique_exact_tip_fallback(
        monkeypatch):
    monkeypatch.setattr(M, "_declared_branch", lambda repo: None)
    calls = []

    def answered(cmd, *a, **k):
        calls.append(cmd)
        return 0, f"legacy {PIN}\nother {'b' * 40}\n", ""

    monkeypatch.setattr(M, "_sh", answered)
    got = M.branch_at_pin("legacy", PIN)
    assert got.status == M.BRANCH_FOUND, got
    assert got.branch == "legacy"
    assert len(calls) == 1


@pytest.mark.parametrize("listing", [
    f"other {'b' * 40}\n",
    f"one {PIN}\ntwo {PIN}\n",
])
def test_missing_declaration_never_guesses_a_descendant_or_ambiguous_tip(
        monkeypatch, listing):
    monkeypatch.setattr(M, "_declared_branch", lambda repo: None)
    monkeypatch.setattr(M, "_sh", lambda *a, **k: (0, listing, ""))
    got = M.branch_at_pin("legacy", PIN)
    assert got.status == M.BRANCH_UNKNOWN, got
    assert got.branch is None


def test_every_registered_fork_has_one_valid_branch_declaration():
    # How many forks there are is README's claim, checked by
    # tools/check_doc_counts.py against this same file; a literal here only
    # fails when a fork is added, which is not a defect.
    entries = M._forks_json().get("forks", [])
    assert entries
    assert all(M._declared_branch(entry["tool"]) == entry.get("branch")
               for entry in entries)


def test_every_pinned_fork_is_registered():
    """A fork the build pins must have a FORKS.json entry, or the round cannot merge it.

    #236 pinned LIBRELANE_REF without one. Its pin was the tip of two
    branches, so `branch_at_pin` could not choose and daily_merge reported
    `librelane BRANCH_LOOKUP_UNKNOWN` -- NEEDS HUMAN, run_tick exit 4, and the
    2026-09-26 00:44 round released nothing. The suite was green on that tree:
    nothing compared what the Dockerfiles pin with what FORKS.json declares.
    """
    pinned = M.build_branches(HERE.parent)
    registered = {entry["tool"] for entry in M._forks_json().get("forks", [])}
    assert pinned, "build_branches found no pins -- the check measured nothing"
    missing = sorted(set(pinned) - registered)
    assert not missing, (
        f"pinned by the build but not in FORKS.json: {missing} -- add an entry "
        "with its branch and upstream, or daily_merge cannot merge it")


def _real_pinned_registry_cases():
    """Use the checked-in build pins and branch registry, not a parallel list."""
    pins = M.build_branches(HERE.parent)
    declarations = {
        entry["tool"]: entry["branch"]
        for entry in M._forks_json()["forks"]
    }
    cases = [
        pytest.param(repo, pin, declarations[repo], id=repo)
        for repo, pin in sorted(pins.items()) if pin
    ]
    assert cases, "the real build-pin corpus must not be empty"
    return cases


@pytest.mark.parametrize("repo,pin,declared", _real_pinned_registry_cases())
@pytest.mark.parametrize("fails_at", ["branches", "compare"])
def test_real_pin_corpus_recovers_truncated_get_on_declared_branch(
        monkeypatch, repo, pin, declared, fails_at):
    """Every real build line survives a blip without changing branch identity."""
    calls = []
    failed_endpoint_calls = []
    tip = "f" * 40 if pin != "f" * 40 else "e" * 40
    branch_path = f"repos/vibeic/{repo}/branches?per_page=100"
    compare_path = (f"repos/vibeic/{repo}/compare/"
                    f"{quote(pin, safe='')}...{quote(declared, safe='')}")

    def answer(cmd, timeout=None, **_kwargs):
        assert cmd[:2] == ["gh", "api"], "no non-read command is expected"
        path = next(arg for arg in cmd if arg.startswith("repos/"))
        assert path in (branch_path, compare_path), cmd
        call = (list(cmd), timeout)
        calls.append(call)
        is_branch = path == branch_path
        if is_branch == (fails_at == "branches"):
            failed_endpoint_calls.append(call)
            if len(failed_endpoint_calls) == 1:
                # Even plausible output is not evidence after rc1. A partial
                # listing omits the declared line; a failed compare appears
                # to refute containment. Neither may become a verdict.
                partial = (f"orphan-topic {pin}\n" if is_branch
                           else json.dumps({"status": "diverged"}))
                return 1, partial, "unexpected end of JSON input"
        if is_branch:
            return 0, f"orphan-topic {pin}\n{declared} {tip}\n", ""
        return 0, json.dumps({"status": "ahead", "total_commits": 1}), ""

    monkeypatch.setattr(M, "_sh", answer)
    got = M.branch_at_pin(repo, pin)

    assert got.status == M.BRANCH_FOUND, got
    assert got.branch == declared, got
    assert "contains pin" in got.detail
    assert len(failed_endpoint_calls) == 2, failed_endpoint_calls
    assert failed_endpoint_calls[0] == failed_endpoint_calls[1]
    assert len(calls) == 3, calls
    for cmd, timeout in calls:
        assert cmd[-2:] == ["--method", "GET"]
        if branch_path in cmd:
            assert "--paginate" in cmd
            assert cmd[cmd.index("--jq") + 1] == '.[] | .name + " " + .commit.sha'
            assert timeout == 180
        else:
            assert timeout == 120


def test_main_persistent_truncated_listing_is_unknown_and_never_merged(
        tmp_path, monkeypatch, capsys):
    """Exercise main plus the real lookup; complete-looking partial rows fail closed."""
    pins = M.build_branches(HERE.parent)
    repo, pin = next((name, sha) for name, sha in sorted(pins.items()) if sha)
    declared = M._declared_branch(repo)
    assert declared
    calls = []
    sleeps = []
    record = tmp_path / "truncated-daily-merge.json"

    def truncated(cmd, timeout=None, **_kwargs):
        calls.append((list(cmd), timeout))
        assert cmd[:2] == ["gh", "api"]
        assert f"repos/vibeic/{repo}/branches?per_page=100" in cmd
        # A complete-looking declared branch must still be discarded on rc1.
        return 1, f"{declared} {pin}\n", "unexpected end of JSON input"

    def must_not_merge(*_args, **_kwargs):
        raise AssertionError("UNKNOWN branch evidence must not reach merge_one")

    monkeypatch.setattr(M, "_sh", truncated)
    monkeypatch.setattr(M, "merge_one", must_not_merge)
    monkeypatch.setattr(M.time, "sleep", sleeps.append)
    rc = M.main(["--eda-root", str(HERE.parent), "--only", repo,
                 "--json", str(record)])

    assert rc == M.RC_NEEDS_HUMAN
    row, = json.loads(record.read_text())["forks"]
    assert row["repo"] == repo
    assert row["state"] == "BRANCH_LOOKUP_UNKNOWN", row
    assert row["branch"] == ""
    assert row["branch_lookup"] == {
        "status": M.BRANCH_UNKNOWN, "rc": 1, "pin": pin,
    }
    assert row["took"] == 0 and row["checks"] == []
    assert len(calls) == 3, calls
    assert all(call == calls[0] for call in calls)
    assert calls[0][0][-2:] == ["--method", "GET"]
    assert calls[0][1] == 180
    assert sleeps == [5.0, 5.0]
    diagnostics = capsys.readouterr().err + row["detail"]
    for number in (1, 2, 3):
        assert f"attempt {number}/3" in diagnostics


def test_unknown_is_a_named_json_row_and_blocks_the_round(tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(M, "build_branches", lambda root: {"yosys": PIN})
    monkeypatch.setattr(
        M, "branch_at_pin",
        lambda repo, pin: M.BranchLookup(
            M.BRANCH_UNKNOWN, detail="GitHub lookup did not answer", rc=127,
        ),
    )
    monkeypatch.setattr(
        M, "merge_one",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("an unresolved branch must not be merged")
        ),
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    assert rc == M.RC_NEEDS_HUMAN
    row = json.loads(record.read_text())["forks"][0]
    assert row["repo"] == "yosys"
    assert row["state"] == "BRANCH_LOOKUP_UNKNOWN", row
    assert row["branch_lookup"]["status"] == M.BRANCH_UNKNOWN, row
    assert row["branch_lookup"]["rc"] == 127, row
    assert row["state"] != "NO_BRANCH_AT_PIN", row


def test_unknown_does_not_disappear_when_another_fork_is_current(
        tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(
        M, "build_branches", lambda root: {"unknown": PIN, "healthy": PIN},
    )

    def lookup(repo, pin):
        if repo == "unknown":
            return M.BranchLookup(
                M.BRANCH_UNKNOWN, detail="transport unavailable", rc=127,
            )
        return M.BranchLookup(M.BRANCH_FOUND, branch="main", rc=0)

    monkeypatch.setattr(M, "branch_at_pin", lookup)
    monkeypatch.setattr(
        M, "merge_one",
        lambda repo, branch, dry, expected_pin="": {
            "repo": repo, "branch": branch, "state": "ALREADY_CURRENT",
            "detail": "level with upstream/main", "conflicts": [],
            "checks": [], "took": 0, "expected_pin": expected_pin,
        },
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    rows = json.loads(record.read_text())["forks"]
    assert rc == M.RC_NEEDS_HUMAN
    assert {(row["repo"], row["state"]) for row in rows} == {
        ("unknown", "BRANCH_LOOKUP_UNKNOWN"),
        ("healthy", "ALREADY_CURRENT"),
    }
    healthy = next(row for row in rows if row["repo"] == "healthy")
    assert healthy["expected_pin"] == PIN


def test_authoritative_absence_is_also_retained_and_blocks(
        tmp_path, monkeypatch):
    record = tmp_path / "daily-merge.json"
    monkeypatch.setattr(M, "build_branches", lambda root: {"yosys": PIN})
    monkeypatch.setattr(
        M, "branch_at_pin",
        lambda repo, pin: M.BranchLookup(
            M.BRANCH_ABSENT, detail="complete listing had no match", rc=0,
        ),
    )

    rc = M.main(["--eda-root", str(tmp_path), "--json", str(record)])

    row = json.loads(record.read_text())["forks"][0]
    assert rc == M.RC_NEEDS_HUMAN
    assert row["state"] == "NO_BRANCH_AT_PIN", row
    assert row["branch_lookup"]["status"] == M.BRANCH_ABSENT, row


def _merge_identity_probe(tmp_path, monkeypatch, identity_rc):
    repo = "yosys"
    src = tmp_path / repo
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(M, "FORKS_ROOT", tmp_path)
    monkeypatch.setattr(M, "_declared_branch", lambda name: "main")
    calls = []

    def fake(cmd, *args, **kwargs):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "remote get-url upstream" in joined:
            return 0, "https://github.com/YosysHQ/yosys.git\n", ""
        if "ls-remote --symref upstream HEAD" in joined:
            return 0, ("ref: refs/heads/master\tHEAD\n"
                       f"{ORIGIN_TIP}\tHEAD\n"), ""
        if "rev-parse --is-shallow-repository" in joined:
            return 0, "false\n", ""
        if "rev-parse --verify refs/remotes/upstream/master^{commit}" in joined:
            return 0, f"{ORIGIN_TIP}\n", ""
        if "rev-parse --verify refs/remotes/origin/main^{commit}" in joined:
            return 0, f"{ORIGIN_TIP}\n", ""
        if "merge-base --is-ancestor" in joined:
            return identity_rc, "", ("missing object" if identity_rc > 1 else "")
        if f"rev-list --count HEAD..{ORIGIN_TIP}" in joined:
            return 0, "0\n", ""
        return 0, "", ""

    monkeypatch.setattr(M, "_sh", fake)
    result = M.merge_one(repo, "main", checks=[], expected_pin=PIN)
    return result, calls


def test_force_move_after_lookup_blocks_before_worktree_or_push(
        tmp_path, monkeypatch):
    result, calls = _merge_identity_probe(tmp_path, monkeypatch, identity_rc=1)
    assert result["state"] == "BRANCH_IDENTITY_CHANGED", result
    assert result["branch_identity"]["outcome"] == "changed"
    assert not any("worktree add" in " ".join(call) for call in calls)
    assert not any(" push " in f" {' '.join(call)} " for call in calls)


def test_unmeasurable_identity_is_not_reported_as_a_changed_branch(
        tmp_path, monkeypatch):
    result, calls = _merge_identity_probe(tmp_path, monkeypatch, identity_rc=128)
    assert result["state"] == "BRANCH_IDENTITY_UNMEASURABLE", result
    assert result["branch_identity"]["outcome"] == "unmeasurable"
    assert not any("worktree add" in " ".join(call) for call in calls)


def test_fresh_identity_is_reproved_before_worktree_creation(
        tmp_path, monkeypatch):
    result, calls = _merge_identity_probe(tmp_path, monkeypatch, identity_rc=0)
    assert result["state"] == "ALREADY_CURRENT", result
    identity_i = next(i for i, call in enumerate(calls)
                      if "merge-base --is-ancestor" in " ".join(call))
    worktree_i = next(i for i, call in enumerate(calls)
                      if "worktree add" in " ".join(call))
    assert identity_i < worktree_i


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}: {proc.stderr}"
    )
    return proc.stdout.strip()


def _publication_race_fleet(tmp_path):
    """Return one fork where origin/main=A and upstream/master=A->U.

    P->A gives the race two meaningful remote replacements: A->B is a
    concurrent advance, while A->P is a rewind that an ordinary push of U
    would silently overwrite as a valid P->U fast-forward.
    """
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "--bare", "-b", "master", str(upstream))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "master")
    _git(seed, "config", "user.name", "race-test")
    _git(seed, "config", "user.email", "race@test.invalid")
    (seed / "history").write_text("P\n")
    _git(seed, "add", "history")
    _git(seed, "commit", "-m", "P")
    rewind = _git(seed, "rev-parse", "HEAD")
    (seed / "history").write_text("P\nA\n")
    _git(seed, "commit", "-am", "A")
    observed = _git(seed, "rev-parse", "HEAD")

    _git(seed, "remote", "add", "origin-race", str(origin))
    _git(seed, "remote", "add", "upstream-race", str(upstream))
    _git(seed, "push", "origin-race", "HEAD:refs/heads/main")
    _git(seed, "push", "upstream-race", "HEAD:refs/heads/master")

    # The upstream change daily_merge should publish.
    (seed / "upstream-only").write_text("U\n")
    _git(seed, "add", "upstream-only")
    _git(seed, "commit", "-m", "U")
    _git(seed, "push", "upstream-race", "HEAD:refs/heads/master")

    # A different actor's legitimate A->B advance, retained as an object in
    # origin so update-ref can install it immediately before our push.
    _git(seed, "checkout", "--detach", observed)
    (seed / "concurrent-only").write_text("B\n")
    _git(seed, "add", "concurrent-only")
    _git(seed, "commit", "-m", "B")
    advance = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin-race", "HEAD:refs/heads/race-object")

    forks_root = tmp_path / "forks"
    forks_root.mkdir()
    src = forks_root / "yosys"
    _git(tmp_path, "clone", "--quiet", str(origin), str(src))
    _git(src, "remote", "add", "upstream", str(upstream))
    _git(src, "fetch", "--quiet", "upstream")
    _git(src, "remote", "set-head", "upstream", "master")
    return forks_root, origin, observed, {"advance": advance, "rewind": rewind}


def test_live_default_branch_replaces_a_valid_but_stale_cached_head(
        tmp_path, monkeypatch):
    """A resolving upstream/HEAD is still only cache, not remote evidence.

    This is the exact Trilinos failure: the checkout retained master while the
    remote moved HEAD to develop.  The old implementation returned
    ALREADY_CURRENT and took zero; the live branch actually had one commit.
    """
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "--bare", "-b", "master", str(upstream))

    seed = tmp_path / "seed-default-change"
    seed.mkdir()
    _git(seed, "init", "-b", "master")
    _git(seed, "config", "user.name", "default-branch-test")
    _git(seed, "config", "user.email", "default-branch@test.invalid")
    (seed / "base").write_text("base\n")
    _git(seed, "add", "base")
    _git(seed, "commit", "-m", "base")
    observed = _git(seed, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin-test", str(origin))
    _git(seed, "remote", "add", "upstream-test", str(upstream))
    _git(seed, "push", "origin-test", "HEAD:refs/heads/main")
    _git(seed, "push", "upstream-test", "HEAD:refs/heads/master")

    forks_root = tmp_path / "forks-default-change"
    forks_root.mkdir()
    src = forks_root / "Trilinos"
    _git(tmp_path, "clone", "--quiet", str(origin), str(src))
    _git(src, "remote", "add", "upstream", str(upstream))
    _git(src, "fetch", "--quiet", "upstream")
    _git(src, "remote", "set-head", "upstream", "master")
    assert _git(src, "symbolic-ref", "refs/remotes/upstream/HEAD").endswith(
        "/master")

    _git(seed, "checkout", "-b", "develop")
    (seed / "develop-only").write_text("new default\n")
    _git(seed, "add", "develop-only")
    _git(seed, "commit", "-m", "develop becomes default")
    develop_tip = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "upstream-test", "HEAD:refs/heads/develop")
    _git(upstream, "symbolic-ref", "HEAD", "refs/heads/develop")

    monkeypatch.setattr(M, "FORKS_ROOT", forks_root)
    monkeypatch.setattr(M, "_declared_branch", lambda repo: "main")
    result = M.merge_one(
        "Trilinos", "main", dry=False, checks=[], expected_pin=observed,
    )

    assert result["state"] == "MERGED", result
    assert result["took"] == 1
    assert result["upstream_default"] == {
        "branch": "develop",
        "remote_tip": develop_tip,
        "tracking_tip": develop_tip,
    }
    published = _git(origin, "rev-parse", "refs/heads/main")
    _git(origin, "merge-base", "--is-ancestor", develop_tip, published)


@pytest.mark.parametrize("race", ["advance", "rewind"])
def test_concurrent_remote_move_is_rejected_by_exact_tip_lease(
        tmp_path, monkeypatch, race):
    forks_root, origin, observed, replacements = _publication_race_fleet(tmp_path)
    replacement = replacements[race]
    real_sh = M._sh
    pushes = []

    def race_immediately_before_publish(cmd, *args, **kwargs):
        if len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
            pushes.append(list(cmd))
            # This is the remote actor racing after our fetch/checks.  It does
            # not update the shared checkout's origin/main tracking ref.
            _git(origin, "update-ref", "refs/heads/main", replacement, observed)
        return real_sh(cmd, *args, **kwargs)

    monkeypatch.setattr(M, "FORKS_ROOT", forks_root)
    monkeypatch.setattr(M, "_declared_branch", lambda repo: "main")
    monkeypatch.setattr(M, "_sh", race_immediately_before_publish)

    result = M.merge_one(
        "yosys", "main", dry=False, checks=[], expected_pin=observed,
    )

    assert result["state"] == "PUSH_CAS_REJECTED", result
    assert result["state"] != "MERGED"
    assert result["publication"] == {
        "mode": "explicit-force-with-lease-cas",
        "ref": "refs/heads/main",
        "expected_old_tip": observed,
        "outcome": "rejected",
        "classification": "needs_human",
        "rc": 1,
        "diagnostic": result["publication"]["diagnostic"],
    }
    assert "stale info" in result["publication"]["diagnostic"], result
    assert _git(origin, "rev-parse", "refs/heads/main") == replacement
    assert len(pushes) == 1
    assert (f"--force-with-lease=refs/heads/main:{observed}"
            in pushes[0]), pushes[0]
    assert "HEAD:refs/heads/main" in pushes[0]
