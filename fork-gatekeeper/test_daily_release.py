#!/usr/bin/env python3
"""Tests for the pin -> artefact -> image release chain.

WHY THESE EXIST
===============
`daily_release.py` shipped five defects in one day, each of which made a broken
state report as a clean one:

  1. pins resolved through a branch RANKING with no containment check, proposing
     to move cadical's pin onto an upstream `sweep` branch;
  2. rebuild triggered on "the pin moved during this run" instead of "the
     artefact is absent", so an interrupted run never rebuilt;
  3. the same hole one level up — the composed IMAGE was not part of the
     absence test;
  4. `VERSION` was bumped from a file that had drifted (0.2.30 on disk, 0.2.32
     built), so the next number collided with an existing image;
  5. the compose tagged one image and the smoke read another, so the release
     would have published older bytes under the new number.

Four of the five were found by reading output or source, not by any test. Each
test below drives a real function against a real fixture tree and asserts the
observable result — never that the source contains a string, which is how a
gate stays green through a runtime failure on the line it claims to check.
"""
from __future__ import annotations

import json
import ast
import pathlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pins_current as C                                # noqa: E402
import daily_release as R                                     # noqa: E402
import fork_reaches_flow_check as F                           # noqa: E402


def _tree(tmp_path: Path) -> Path:
    """A miniature eda-root: two single-repo tools and one two-repo tool."""
    root = tmp_path / "eda"
    (root / "tools" / "yosys").mkdir(parents=True)
    (root / "tools" / "lvs").mkdir(parents=True)
    (root / "tools" / "yosys" / "Dockerfile").write_text(
        "ARG YOSYS_REPO=https://github.com/vibeic/yosys.git\n"
        "ARG YOSYS_REF=" + "a" * 40 + "  # branch vibeic/integration\n"
        'RUN git clone "${YOSYS_REPO}" /y\n'
        "# github.com/vibeic/yosys \n")
    # one Dockerfile, TWO repos — a first-match parse silently drops one
    (root / "tools" / "lvs" / "Dockerfile").write_text(
        "ARG MAGIC_REF=" + "b" * 40 + "\n"
        "ARG NETGEN_REF=" + "c" * 40 + "\n"
        'RUN git clone https://github.com/vibeic/magic.git /m \\\n'
        ' && git clone https://github.com/vibeic/netgen.git /n\n')
    (root / "Dockerfile").write_text(
        "ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:aaaaaaa\n"
        "ARG IMG_LVS=ghcr.io/vibeic/eda-tool-lvs:bbbbbbb-ccccccc\n"
        "FROM ${IMG_YOSYS} AS img-yosys\n"
        "FROM ${IMG_LVS} AS img-lvs\n"
        "COPY --from=img-yosys /foss/tools/yosys /foss/tools/yosys\n"
        "COPY --from=img-lvs /foss/tools/magic /foss/tools/magic\n")
    (root / "docker-bake.hcl").write_text(
        'variable "YOSYS_REF"  { default = "' + "a" * 40 + '" }\n'
        'variable "MAGIC_REF"  { default = "' + "b" * 40 + '" }\n'
        'variable "NETGEN_REF" { default = "' + "c" * 40 + '" }\n'
        'target "yosys" {\n'
        '  context = "tools/yosys"\n'
        '  tags    = tool_tags("yosys", YOSYS_REF)\n'
        '}\n'
        'target "lvs" {\n'
        '  context = "tools/lvs"\n'
        '  tags    = ["${REGISTRY}/eda-tool-lvs:${short(MAGIC_REF)}-'
        '${short(NETGEN_REF)}"]\n'
        '}\n')
    (root / "VERSION").write_text("0.2.30\n")
    return root


def test_two_repos_in_one_dockerfile_are_both_found(tmp_path):
    """`tools/lvs` pins magic AND netgen; a first-match parse loses one."""
    root = _tree(tmp_path)
    args = R.ref_arg_names(root)
    assert args["magic"] == "MAGIC"
    assert args["netgen"] == "NETGEN", "netgen dropped — the second repo in a file"
    assert args["yosys"] == "YOSYS"


def test_tag_composition_is_read_from_bake_not_assumed(tmp_path):
    """`lvs` is tagged <magic>-<netgen>; bumping magic alone must retag."""
    root = _tree(tmp_path)
    targets = R.bake_targets(root)
    assert targets["lvs"] == ["MAGIC_REF", "NETGEN_REF"], "order matters in the tag"
    assert targets["yosys"] == ["YOSYS_REF"]


def test_rewrite_pin_moves_every_site_and_keeps_the_comment(tmp_path):
    """An earlier bumper rewrote whole lines with sed and ate the comments,
    after which a parser reading the branch FROM the comment lost two tools."""
    root = _tree(tmp_path)
    changed = R.rewrite_pin(root, "YOSYS", "d" * 40)
    assert "tools/yosys/Dockerfile" in changed
    assert "docker-bake.hcl" in changed
    df = (root / "tools" / "yosys" / "Dockerfile").read_text()
    assert "d" * 40 in df
    assert "# branch vibeic/integration" in df, "trailing comment destroyed"
    assert "d" * 40 in (root / "docker-bake.hcl").read_text()


def test_retag_recomposes_a_two_commit_tag(tmp_path):
    """Bumping magic while leaving `eda-tool-lvs:<magic>-<netgen>` untouched
    would keep pulling the artefact built before the bump."""
    root = _tree(tmp_path)
    refs = {"YOSYS_REF": "a" * 40, "MAGIC_REF": "e" * 40, "NETGEN_REF": "c" * 40}
    touched = R.retag_images(root, refs, R.bake_targets(root))
    assert any("IMG_LVS" in t for t in touched)
    assert "eda-tool-lvs:eeeeeee-ccccccc" in (root / "Dockerfile").read_text()


def test_version_rises_above_reality_not_just_the_file(tmp_path, monkeypatch):
    """VERSION read 0.2.30 while 0.2.32 was built; a file-only bump collides."""
    root = _tree(tmp_path)
    monkeypatch.setattr(R, "_existing_versions", lambda _r: [(0, 2, 31), (0, 2, 32)])
    old, new = R.peek_version(root)
    assert (old, new) == ("0.2.30", "0.2.33")
    assert (root / "VERSION").read_text().strip() == "0.2.30", \
        "peek must NOT write — the file moves only after a successful push"


def test_version_rolls_the_minor_at_99(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(R, "_existing_versions", lambda _r: [(0, 2, 99)])
    assert R.peek_version(root)[1] == "0.3.0"


def test_fingerprint_is_stable_and_moves_with_any_pin(tmp_path):
    """RELEASED.json's whole purpose: a pin set that differs has not shipped."""
    a = {"yosys": "a" * 40, "magic": "b" * 40}
    assert R.pins_fingerprint(a) == R.pins_fingerprint(dict(reversed(list(a.items()))))
    assert R.pins_fingerprint(a) != R.pins_fingerprint({**a, "magic": "c" * 40})
    assert R.pins_fingerprint(a) != R.pins_fingerprint({**a, "extra": "d" * 40})


def test_unreleased_when_no_record_and_released_when_it_matches(tmp_path):
    root = _tree(tmp_path)
    pins = {"yosys": "a" * 40}
    assert R.released_record(root) == {}, "no record means never released"
    (root / "RELEASED.json").write_text(json.dumps(
        {"version": "0.2.33", "pins_fingerprint": R.pins_fingerprint(pins)}))
    assert R.released_record(root)["pins_fingerprint"] == R.pins_fingerprint(pins)
    # the failure this guards: one pin moves, the record must no longer match
    assert R.released_record(root)["pins_fingerprint"] != \
        R.pins_fingerprint({"yosys": "z" * 40})


def test_copied_paths_reads_what_came_from_our_artefacts(tmp_path):
    """The containment test in fork_reaches_flow_check depends on this map."""
    root = _tree(tmp_path)
    ours = F.copied_paths(root / "Dockerfile")
    assert ours["yosys"] == ["/foss/tools/yosys"]
    assert ours["lvs"] == ["/foss/tools/magic"]


def test_provenance_drift_reports_not_checked_rather_than_a_finding(monkeypatch):
    """An image with no provenance compared nothing; that is not 1 drift row."""
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, "", ""))
    out = F.provenance_drift("img", {"yosys": "a" * 40})
    assert len(out) == 1 and out[0].get("not_checked") is True


def test_provenance_drift_finds_a_ref_the_pin_no_longer_names(monkeypatch):
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, json.dumps(
        {"tool": "yosys", "repo": "https://github.com/vibeic/yosys.git",
         "ref": "z" * 40}), ""))
    out = F.provenance_drift("img", {"yosys": "a" * 40})
    assert len(out) == 1 and not out[0].get("not_checked")
    assert out[0]["built_from"] == "z" * 9


def test_provenance_drift_is_silent_when_the_image_matches(monkeypatch):
    monkeypatch.setattr(F, "_sh", lambda *a, **k: (0, json.dumps(
        {"tool": "yosys", "repo": "https://github.com/vibeic/yosys.git",
         "ref": "a" * 40}), ""))
    assert F.provenance_drift("img", {"yosys": "a" * 40}) == []


def test_artefact_tag_carries_every_ref_and_the_recipe(tmp_path):
    root = _tree(tmp_path)
    refs = {"MAGIC_REF": "b" * 40, "NETGEN_REF": "c" * 40}
    tag = R.artefact_tag(root, "lvs", ["MAGIC_REF", "NETGEN_REF"], refs)
    assert tag.startswith("ghcr.io/vibeic/eda-tool-lvs:bbbbbbb-ccccccc-")
    assert len(tag.rsplit("-", 1)[1]) == 6, "recipe component missing"


def test_changing_the_recipe_changes_the_tag(tmp_path):
    """#21: a recipe-only fix produced no new tag, so it could never ship."""
    root = _tree(tmp_path)
    refs = {"YOSYS_REF": "a" * 40}
    before = R.artefact_tag(root, "yosys", ["YOSYS_REF"], refs)
    df = root / "tools" / "yosys" / "Dockerfile"
    df.write_text(df.read_text() + "\n# install to a different prefix\n")
    after = R.artefact_tag(root, "yosys", ["YOSYS_REF"], refs)
    assert before != after, "the recipe changed and the artefact identity did not"
    assert before.rsplit("-", 1)[0] == after.rsplit("-", 1)[0], \
        "only the recipe component may move when only the recipe moved"


def test_artefact_tag_is_none_when_a_ref_is_unknown(tmp_path):
    """A tag built from a partial ref set would name the wrong artefact."""
    root = _tree(tmp_path)
    assert R.artefact_tag(root, "lvs", ["MAGIC_REF", "NETGEN_REF"],
                          {"MAGIC_REF": "b" * 40}) is None


def test_recipe_hash_says_so_when_there_is_no_dockerfile(tmp_path):
    root = _tree(tmp_path)
    assert R.recipe_hash(root, "does-not-exist") == "nofile"


def test_bake_and_the_program_agree_on_every_recipe(tmp_path):
    """#21's core: two expressions compose a tool tag and must not diverge."""
    root = _tree(tmp_path)
    hcl = root / "docker-bake.hcl"
    hcl.write_text(hcl.read_text() +
                   '\nvariable "YOSYS_RECIPE" { default = "stale0" }\n')
    moved = R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]})
    assert moved and moved[0].startswith("YOSYS_RECIPE=")
    assert R.bake_recipe_vars(root)["YOSYS"] == R.recipe_hash(root, "yosys")


def test_write_recipe_vars_is_idempotent(tmp_path):
    root = _tree(tmp_path)
    hcl = root / "docker-bake.hcl"
    hcl.write_text(hcl.read_text() +
                   '\nvariable "YOSYS_RECIPE" { default = "stale0" }\n')
    R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]})
    assert R.write_recipe_vars(root, {"yosys": ["YOSYS_REF"]}) == [], \
        "a second pass must report nothing moved"


def test_a_recipe_only_change_makes_the_release_fingerprint_move(tmp_path):
    """Over pins alone, two images with different bytes share a fingerprint."""
    root = _tree(tmp_path)
    pins = {"yosys": "a" * 40}
    before = R.pins_fingerprint({**pins,
                                 "recipe:yosys": R.recipe_hash(root, "yosys")})
    df = root / "tools" / "yosys" / "Dockerfile"
    df.write_text(df.read_text() + "\n# a different prefix\n")
    after = R.pins_fingerprint({**pins,
                                "recipe:yosys": R.recipe_hash(root, "yosys")})
    assert before != after


def test_a_root_dockerfile_change_moves_the_release_fingerprint(tmp_path):
    """#19 and #20 are root-Dockerfile-only fixes; without this they never ship."""
    root = _tree(tmp_path)
    before = R.compose_recipe_hash(root)
    df = root / "Dockerfile"
    df.write_text(df.read_text() + "\n# pin the base image by digest\n")
    assert R.compose_recipe_hash(root) != before


def test_a_bake_change_moves_it_too(tmp_path):
    root = _tree(tmp_path)
    before = R.compose_recipe_hash(root)
    hcl = root / "docker-bake.hcl"
    hcl.write_text(hcl.read_text() + '\nvariable "X" { default = "y" }\n')
    assert R.compose_recipe_hash(root) != before


def test_branch_is_ours_true_when_we_are_ahead(monkeypatch):
    calls = iter([(0, "MikePopoloski/slang", ""), (0, "34", "")])
    monkeypatch.setattr(C, "_sh", lambda *a, **k: next(calls))
    assert R.branch_is_ours("slang", "satfix-integration") is True


def test_branch_is_ours_false_for_a_pure_upstream_mirror(monkeypatch):
    """#23/#25 pinned four tools to what the image ships; master is upstream's."""
    calls = iter([(0, "MikePopoloski/slang", ""), (0, "0", "")])
    monkeypatch.setattr(C, "_sh", lambda *a, **k: next(calls))
    assert R.branch_is_ours("slang", "master") is False


def test_branch_is_ours_is_none_when_it_cannot_tell(monkeypatch):
    """Fail-safe: unknown must not read as ours, or the pin gets advanced."""
    monkeypatch.setattr(C, "_sh", lambda *a, **k: (0, "", ""))
    assert R.branch_is_ours("mirror-repo", "master") is None
    monkeypatch.setattr(C, "_sh", lambda *a, **k: (0, "up/stream", "")
                        if "repos/vibeic/x" == a[0][2] else (1, "", "boom"))
    assert R.branch_is_ours("x", "master") is None


# --- vibeic-eda#26: a wedged build must become a failure, and its log must survive


def test_a_build_that_never_exits_is_reported_as_a_timeout(monkeypatch):
    """The wedge has cost four manual interventions. `subprocess` already knew.

    Four times a compose sat with buildx alive, no compiler running, no disk
    write and no network, and four times the decision to stop it was mine to
    make by hand. The deadline existed the whole time — it was two hours, and
    nobody was watching for two hours.
    """
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker buildx bake", timeout=5400)
    monkeypatch.setattr(R.subprocess, "run", boom)
    rc, out, err = R._sh(["docker", "buildx", "bake"], timeout=5400)
    assert rc == 1
    assert "TIMED OUT" in err and "5400" in err
    # and it must be attributable, not just a failure
    assert "#26" in err


def test_build_env_reaches_the_subprocess_and_keeps_the_inherited_environment(monkeypatch):
    """`env=` REPLACES the environment; the build needs PATH and DOCKER_HOST too."""
    seen = {}

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    def rec(cmd, **k):
        seen.update(k.get("env") or {})
        return P()
    monkeypatch.setenv("A_HOST_VAR_THE_BUILD_NEEDS", "present")
    monkeypatch.setattr(R.subprocess, "run", rec)
    R._sh(["docker", "buildx", "bake"], env=R.BUILD_ENV)
    assert seen.get("BUILDKIT_STEP_LOG_MAX_SIZE") == "-1"
    assert seen.get("A_HOST_VAR_THE_BUILD_NEEDS") == "present", \
        "the inherited environment was dropped — PATH would go with it"


def test_no_env_leaves_the_environment_alone(monkeypatch):
    """Passing env=None must not materialise a copy: the API calls don't need one."""
    seen = {"env": "unset"}

    class P:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(R.subprocess, "run",
                        lambda cmd, **k: (seen.update(env=k.get("env")), P())[1])
    R._sh(["gh", "api", "x"])
    assert seen["env"] is None


def test_every_buildx_invocation_is_bounded_and_carries_the_build_env():
    """WIRING, which is where this class of fix leaks.

    Adding the deadline to `_sh` does nothing for a call site that does not pass
    it, and the two bake calls are the only ones that matter. Stated limit: this
    reads the call sites rather than executing them — a real bake is not a unit
    test — so it can only catch an UNWIRED call, not a mis-wired one. The
    behaviour of what it wires is covered by the two tests above.
    """
    tree = ast.parse(pathlib.Path(R.__file__).read_text())
    # Resolve the two names bound to a bake command list, so the call site that
    # passes a variable is judged the same as the one that passes a literal.
    # `bake`, not `buildx`: the property is about BUILDS. `buildx inspect` is a
    # buildx call that needs neither a build environment nor an hours-long
    # deadline, and an over-broad predicate that flags it would be trained away
    # the first time it fired — which is how a gate stops meaning anything.
    bake_vars = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)
                 and "'bake'" in ast.dump(n.value)}
    calls = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_sh" and n.args):
            continue
        a0 = n.args[0]
        literal = "'bake'" in ast.dump(a0)
        by_name = isinstance(a0, ast.Name) and a0.id in bake_vars
        if literal or by_name:
            calls.append(n)
    assert len(calls) >= 2, f"expected both bake call sites, found {len(calls)}"
    for c in calls:
        kw = {k.arg: ast.dump(k.value) for k in c.keywords}
        assert "BUILD_ENV" in kw.get("env", ""), \
            f"a buildx call at line {c.lineno} passes no BUILD_ENV"
        assert "TIMEOUT" in kw.get("timeout", ""), \
            f"a buildx call at line {c.lineno} has no bounded deadline"


def test_the_deadlines_clear_the_measured_build_times():
    """Set from measurement: composes run 13-20 min, tool builds longer."""
    assert R.COMPOSE_TIMEOUT >= 20 * 60 * 2
    assert R.TOOL_BUILD_TIMEOUT > R.COMPOSE_TIMEOUT


def test_the_log_limit_reports_that_the_docker_driver_cannot_honour_it(monkeypatch):
    """Measured: with the `docker` driver the setting is a no-op, and silently.

    buildkit reads BUILDKIT_STEP_LOG_MAX_SIZE in the DAEMON. With the default
    driver the daemon is dockerd, which does not inherit the client's
    environment — so a release can pass the variable, print nothing, and still
    clip the log of the one step that keeps wedging.
    """
    monkeypatch.setattr(R, "_sh", lambda *a, **k: (0, "Name: default\nDriver: docker\n", ""))
    assert R.log_limit_effective() is False
    monkeypatch.setattr(R, "_sh",
                        lambda *a, **k: (0, "Name: b\nDriver: docker-container\n", ""))
    assert R.log_limit_effective() is True


def test_an_unreadable_driver_is_unknown_not_effective(monkeypatch):
    """Fail-safe direction: unknown must not read as 'the limit is lifted'."""
    monkeypatch.setattr(R, "_sh", lambda *a, **k: (1, "", "no such builder"))
    assert R.log_limit_effective() is None
    monkeypatch.setattr(R, "_sh", lambda *a, **k: (0, "Name: default\n", ""))
    assert R.log_limit_effective() is None


# --- vibeic-eda#17: the capability check was blind to the tool being replaced

def test_a_prefix_moved_aside_counts_as_replaced(tmp_path):
    """`mv` is a replacement. Leaving it out was a real hole.

    #17 replaces the base's klayout by moving its tree to `klayout-base` and
    symlinking the canonical path at ours — no `rm -rf`, no `COPY` to that
    path. The prefix scanner considered `/foss/tools/klayout` untouched, so the
    check compared nothing for the one tool whose replacement was the change.
    """
    import check_no_capability_lost as C
    df = tmp_path / "Dockerfile"
    df.write_text(
        "ARG BASE_IMAGE=example/base:1\n"
        "RUN rm -rf /foss/tools/yosys\n"
        "COPY --from=img-lvs /x /foss/tools/magic\n"
        "RUN mv /foss/tools/klayout /foss/tools/klayout-base \\\n"
        " && ln -s /foss/tools/klayout-vibeic /foss/tools/klayout\n")
    p = C.replaced_prefixes(df)
    assert "klayout" in p, "a prefix moved aside is a prefix replaced"
    assert "yosys" in p and "magic" in p, "the existing detections still work"


def test_commands_are_listed_at_the_prefix_root_not_only_in_bin(tmp_path,
                                                                monkeypatch):
    """`<prefix>/bin` is a layout assumption, not a rule.

    yosys and magic put binaries in `bin/`; klayout puts `klayout` and twelve
    `strm2*` buddies at the top of its prefix. Measured before this fix: 55
    commands compared, `klayout` in none of them.
    """
    import check_no_capability_lost as C
    seen = {}

    def fake(cmd, timeout=600):
        seen["script"] = cmd[-1]
        return 0, "klayout\nstrm2gds\nyosys\n", ""
    monkeypatch.setattr(C, "_sh", fake)
    names = C.command_names("img", ["klayout", "yosys"])
    assert "klayout" in names and "strm2gds" in names
    s = seen["script"]
    assert "/foss/tools/klayout/bin" in s, "the bin/ layout must still be listed"
    assert "-maxdepth 1 -type f -executable" in s, \
        "executables at the prefix root are not listed at all"
    assert '! -name "*.so*"' in s, \
        "without this, version-suffixed sonames compare as commands and every " \
        "version bump reports a loss"


# --- a branch that exists only in our fork is OURS, and unknown is not a negative

def test_a_branch_absent_upstream_is_conclusively_ours(monkeypatch):
    """This is the case the predicate was worst at, and it is the common one.

    The comparison is `upstream:<branch>...vibeic:<branch>`. Every one of our
    integration branches — `yosys satfix-integration`,
    `klayout vibeic/klayout-signoff-int`, `magic vibeic/integration` — exists
    nowhere but our fork, so the compare 404s and the answer was "could not
    tell" about branches that could not be more ours. The docstring even
    recorded "yosys satfix-integration carries 34", a measurement the code as
    written could no longer reproduce.
    """
    calls = iter([
        (0, "YosysHQ/yosys", ""),      # parent
        (1, "", "404"),                # compare fails
        (1, "", "404"),                # upstream has no such branch
        (0, "satfix-integration", ""),  # we do
    ])
    monkeypatch.setattr(C, "_sh", lambda *a, **k: next(calls))
    assert C.branch_is_ours("yosys", "satfix-integration") is True


def test_a_transport_failure_is_still_unknown(monkeypatch):
    """The fail-safe must stay reachable. Upstream branch readable, ours not —
    that is a broken measurement, not evidence of anything."""
    calls = iter([
        (0, "YosysHQ/yosys", ""),
        (1, "", "boom"),
        (0, "master", ""),   # upstream HAS the branch
        (1, "", "boom"),     # and we could not read ours
    ])
    monkeypatch.setattr(C, "_sh", lambda *a, **k: next(calls))
    assert C.branch_is_ours("yosys", "master") is None


def test_unknown_is_not_reported_as_a_negative_finding():
    """WIRING. `if ours: ... else: ...` put None in with False and printed
    "that branch carries none of our commits" — a statement of fact asserted
    from a `None`.

    Stated limit: this reads the decision's SHAPE rather than running the
    release, which needs a registry and an hour. It can only catch the two
    outcomes sharing one arm, which is exactly how the defect was written.
    """
    import ast, pathlib
    tree = ast.parse(pathlib.Path(R.__file__).read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "ours"):
            continue
        arms = ast.dump(ast.Module(body=node.orelse, type_ignores=[]))
        assert "ours" in arms and "False" in arms, (
            "the not-taken side does not distinguish False from None, so "
            "'could not tell' is reported as 'found to be upstream's'")
        return
    raise AssertionError("no `if ours:` decision found — test is stale")


def test_the_recorded_fingerprint_matches_the_tree_that_shipped(tmp_path):
    """The record has to be reproducible from what shipped, or every run releases.

    `fp` is measured early, to DECIDE whether to release. `rewrite_pin` and
    `retag_images` then edit the root Dockerfile, and that file feeds
    `compose_recipe_hash`. Recording the early value made the record
    irreproducible: measured on 0.2.45, the released tree computes e4e0a5f6
    while the file it shipped records 94d85fda, and the next run began composing
    0.2.46 with nothing changed at all — the program's own refusal to cut a
    contentless version, defeated by the order of two writes.

    This drives the real functions against a real tree: fingerprint, edit the
    Dockerfile the way retag_images does, fingerprint again.
    """
    root = _tree(tmp_path)

    def fingerprint():
        targets = R.bake_targets(root)
        return R.pins_fingerprint({
            **R.pinned_refs(root),
            **{f"recipe:{k}": R.recipe_hash(root, k) for k in targets},
            "recipe:__compose__": R.compose_recipe_hash(root)})

    before = fingerprint()
    R.retag_images(root, {"YOSYS_REF": "a" * 40, "MAGIC_REF": "e" * 40,
                          "NETGEN_REF": "c" * 40}, R.bake_targets(root))
    after = fingerprint()
    assert before != after, (
        "retag_images did not move the fingerprint in this fixture, so the "
        "test cannot show the ordering defect — fixture is stale")
    # The property: what gets RECORDED must be the second one. Recording the
    # first is what puts the release on a treadmill.
    assert fingerprint() == after, "the fingerprint must be stable once edits stop"


# --- vibeic-eda#17: a checker that cries wolf gets ignored

def _fake_docker(out_by_script):
    """Answer each `docker run ... -lc <script>` from a prefix->output map."""
    def run(cmd, timeout=600):
        script = cmd[-1]
        for key, val in out_by_script.items():
            if key in script:
                return 0, val, ""
        return 0, "", ""
    return run


def test_yices2_is_not_a_command_and_never_was(monkeypatch):
    """The target ships four binaries and none is called `yices2`.

    Every run reported "yices2: not on PATH" about a tool that is present, ours
    and symlinked four ways — measured, all four resolve into /foss/tools/yices,
    copied from img-yices2, provenance naming vibeic/yices2 at the pin. A finding
    that is always there and always wrong trains everyone to skim the report.
    """
    assert "yices2" in F._MULTI_BINARY
    assert F._MULTI_BINARY["yices2"] == ("yices", "yices-sat", "yices-smt",
                                         "yices-smt2")


def test_a_deliberate_non_install_is_not_a_finding(tmp_path, monkeypatch):
    """sv-elab installs no command ON PURPOSE (vibeic-eda#24)."""
    df = tmp_path / "Dockerfile"
    df.write_text("COPY --from=img-sv-elab /x /foss/tools/slang-yosys-plugin\n"
                  "COPY --from=img-yosys /y /foss/tools/yosys\n")
    monkeypatch.setattr(F, "_sh", _fake_docker({
        "command -v": "sv-elab NONE\nyosys /foss/tools/yosys/bin/yosys "
                      "/foss/tools/yosys/bin/yosys\n"}))
    findings = F.check("img", df)
    assert not [f for f in findings if f["tool"] == "sv-elab"], \
        "an intended state reported forever is a finding nobody reads"


def test_it_still_fires_when_a_tool_we_build_is_not_the_one_that_runs(tmp_path,
                                                                     monkeypatch):
    """The power the fix must NOT cost.

    Verified against real images too: the same corrected checker still reports
    klayout on 0.2.44 and 0.2.39, and only 0.2.45 — the release that swapped the
    prefix — comes back clean.
    """
    df = tmp_path / "Dockerfile"
    df.write_text("COPY --from=img-klayout /b /foss/tools/klayout-vibeic\n")
    monkeypatch.setattr(F, "_sh", _fake_docker({
        "command -v": "klayout /foss/tools/klayout/klayout "
                      "/foss/tools/klayout/klayout\n"}))
    findings = F.check("img", df)
    bad = [f for f in findings if f["tool"] == "klayout"]
    assert bad and bad[0]["we_build_it"], \
        "the check lost the defect it exists to find"


def test_a_source_installed_tool_needs_its_source_tree_present(tmp_path,
                                                               monkeypatch):
    """sby lands in /usr/local/bin via `make install`, which path containment
    cannot attribute. The weaker claim — our source tree is in the image — is
    made explicitly, and its absence is still a finding."""
    df = tmp_path / "Dockerfile"
    df.write_text("COPY --from=img-yosys /y /foss/tools/yosys\n")
    monkeypatch.setattr(F, "_sh", _fake_docker({
        "command -v": "sby /usr/local/bin/sby /usr/local/bin/sby\n",
        "-d /opt/vibeic-forks/sby": "sby\n"}))
    assert not [f for f in F.check("img", df) if f["tool"] == "sby"]

    monkeypatch.setattr(F, "_sh", _fake_docker({
        "command -v": "sby /usr/local/bin/sby /usr/local/bin/sby\n"}))
    bad = [f for f in F.check("img", df) if f["tool"] == "sby"]
    assert bad and "did not come from us" in bad[0]["problem"]


# --- vibeic-eda#29: a gate that fails on a recorded decision is a gate nobody reads

def _pin_check(monkeypatch, status, behind, ours):
    """Drive the real check_one with a fabricated compare and ownership."""
    import check_pins_current as C
    monkeypatch.setattr(C, "_branches", lambda repo: [("master", "sha")])
    monkeypatch.setattr(C, "_gh", lambda path, jq=None, paginate=False:
                        json.dumps({"status": status, "total_commits": behind}))
    monkeypatch.setattr(C, "branch_is_ours", lambda repo, branch: ours)
    return C.check_one("Xyce", "a" * 40)


def test_a_pin_held_on_upstream_history_is_a_decision_not_a_defect(monkeypatch):
    """Four pins sit on pure upstream mirrors DELIBERATELY (#23/#25).

    Reported as STALE they made this program permanently red — 288 commits and
    growing — so rc=1 became the expected value and a real stale pin arriving
    tomorrow would change nothing anyone noticed.
    """
    r = _pin_check(monkeypatch, "ahead", 182, False)
    assert r["verdict"] == "UPSTREAM_AVAILABLE"
    assert "CHOOSE to adopt" in r["detail"]


def test_a_pin_behind_our_own_branch_still_fails(monkeypatch):
    """The teeth this must not cost.

    Verified against the live repository too: rolling the yosys pin back onto
    an older commit of `satfix-integration` — our own branch — reports STALE and
    exits 1.
    """
    r = _pin_check(monkeypatch, "ahead", 35, True)
    assert r["verdict"] == "STALE"
    assert "OURS" in r["detail"]


def test_undecidable_ownership_keeps_the_finding_but_not_the_claim(monkeypatch):
    """Unknown is not a pass, and it is not a diagnosis either."""
    r = _pin_check(monkeypatch, "ahead", 7, None)
    assert r["verdict"] == "STALE_UNDECIDED"
    assert "could not be determined" in r["detail"]
    assert "OURS" not in r["detail"]


def test_the_two_programs_share_one_implementation_of_the_question():
    """Two copies of this answer is how they came to disagree about four pins."""
    import check_pins_current as C
    assert R.branch_is_ours is C.branch_is_ours
