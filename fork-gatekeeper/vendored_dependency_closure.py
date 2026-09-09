#!/usr/bin/env python3
"""Close fork delivery edges that are carried through another fork's gitlink.

``daily_merge`` advances every declared fork branch, but a vendored fork reaches
the image only through its host repository's submodule pointer.  Advancing the
child branch without advancing that pointer therefore leaves a real release gap
which ``daily_release`` cannot see: the host branch (and consequently its image
pin) did not move.

This program runs immediately after ``daily_merge``.  It derives the delivery
graph from the Dockerfiles and each published host tree, compares every vibeic
submodule gitlink with the declared published tip of that child fork, and makes
the smallest deterministic host commit needed to close the edge.  Publication
uses an exact force-with-lease compare-and-swap and is followed by remote
readback.  Host self-checks run on the candidate, with the changed submodules
materialised, before anything is pushed.

The scan repeats to a fixed point so a nested child update can advance its host,
then advance that host's parent on the next pass.  A divergent child, an
unmeasured branch, a failed host check, or an unconfirmed push stops release;
none is converted into a clean zero.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import daily_merge
import discover_forks


RC_OK, RC_NEEDS_HUMAN, RC_UNMEASURED = 0, 1, 2
PROGRAM = "vendored_dependency_closure"
SCHEMA_VERSION = 1
OID = re.compile(r"[0-9a-fA-F]{40,64}")
VALID_BRANCH = re.compile(r"(?!-)(?!/)(?!.*\.\.)(?!.*[~^:?*\[\\])[^\s]+")


def _run(args: List[str], cwd: Optional[Path] = None,
         timeout: int = 300) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timed out"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _tail(text: str, limit: int = 400) -> str:
    """The END of a tool's error, not its beginning.

    MEASURED 2026-09-05. `git submodule update` failed twice for OpenSTA and both
    times the recorded detail was exactly

        could not materialise src/sta at 34bf022ea6f1: Cloning into '/tmp/vendored_closure_OpenROAD_b2kcey3j/wt/src/sta'...

    which is git's PROGRESS line, not its failure. Every truncation in this file
    took the first N characters, and a clone announces itself before it fails, so
    the announcement is all that ever survived -- in the log, in the .txt sidecar
    and in the JSON alike. Three rounds refused to publish on a diagnosis nobody
    could read.

    git puts the reason last ("fatal: ...", "error: ..."), so keep the tail, and
    prefer whole lines carrying a recognised marker when there are any.
    """
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    if len(text) <= limit:
        return text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    flagged = [ln for ln in lines
               if any(k in ln.lower() for k in
                      ("fatal", "error", "cannot", "could not", "denied",
                       "timed out", "unable", "refused", "not found"))]
    picked = " | ".join(flagged[-4:]) if flagged else " | ".join(lines[-4:])
    if len(picked) > limit:
        picked = "..." + picked[-(limit - 3):]
    return picked


def _git(repo: Path, *args: str, timeout: int = 300) -> Tuple[int, str, str]:
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _registry(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"could not read {path}: {exc}"
    rows = value.get("forks") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return None, f"{path} has no forks list"
    declared: Dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("tool"), str):
            return None, f"{path} contains a malformed fork row"
        tool = row["tool"]
        branch = row.get("branch")
        if (not isinstance(branch, str) or not VALID_BRANCH.fullmatch(branch)
                or branch.endswith(("/", "."))):
            return None, f"{tool} has no valid declared published branch"
        if tool.lower() in declared:
            return None, f"{path} declares {tool} more than once"
        declared[tool.lower()] = row
    return declared, None


def _direct_recursive_hosts(eda_root: Path) -> Tuple[Optional[Dict[str, dict]], Optional[str]]:
    hosts: Dict[str, dict] = {}
    dockerfiles = sorted((eda_root / "tools").glob("*/Dockerfile"))
    dockerfiles.append(eda_root / "Dockerfile")
    for dockerfile in dockerfiles:
        if not dockerfile.is_file():
            continue
        try:
            pins = discover_forks.parse_dockerfile_pins(
                dockerfile.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # the parser forming no answer is not no edges
            return None, f"could not parse {dockerfile}: {exc}"
        for pin in pins.values():
            if not pin.get("submodules"):
                continue
            repo = pin.get("repo")
            if not isinstance(repo, str) or not repo:
                return None, f"{dockerfile} has a recursive pin without a repository"
            key = repo.lower()
            candidate = {
                "repo": repo,
                "recursive": bool(pin.get("recursive")),
                "source": str(dockerfile.relative_to(eda_root)),
            }
            if key in hosts and hosts[key] != candidate:
                return None, f"conflicting recursive delivery declarations for {repo}"
            hosts[key] = candidate
    return hosts, None


def _clone_path(forks_root: Path, repo: str) -> Path:
    return forks_root / repo


def _observe_branch(forks_root: Path, repo: str, branch: str) -> Tuple[Optional[dict], Optional[str]]:
    clone = _clone_path(forks_root, repo)
    if not (clone / ".git").exists() and not (clone / "HEAD").exists():
        return None, f"fork clone {clone} is missing"
    rc, _, err = _git(clone, "remote", "set-branches", "origin", "*")
    if rc != 0:
        return None, f"could not widen {repo} origin refspec: {_tail(err or '')}"
    rc, _, err = _git(clone, "fetch", "origin", "--prune", timeout=1800)
    if rc != 0:
        return None, f"could not fetch vibeic/{repo}: {_tail(err or '')}"
    ref = f"refs/heads/{branch}"
    rc, out, err = _git(clone, "ls-remote", "--refs", "origin", ref)
    rows = [line.split() for line in out.splitlines() if line.strip()]
    if (rc != 0 or len(rows) != 1 or len(rows[0]) != 2
            or not OID.fullmatch(rows[0][0]) or rows[0][1] != ref):
        detail = " ".join((err or out).split())
        return None, (f"remote vibeic/{repo} {ref} did not resolve to exactly "
                      f"one commit" + (f": {_tail(detail)}" if detail else ""))
    remote_tip = rows[0][0].lower()
    rc, out, err = _git(
        clone, "rev-parse", "--verify",
        f"refs/remotes/origin/{branch}^{{commit}}",
    )
    local_rows = [line.strip().lower() for line in out.splitlines() if line.strip()]
    if (rc != 0 or local_rows != [remote_tip]):
        detail = " ".join((err or out).split())
        return None, (f"fresh origin/{branch} for {repo} does not match remote "
                      f"tip {remote_tip[:12]}"
                      + (f": {_tail(detail)}" if detail else ""))
    return {"clone": clone, "branch": branch, "tip": remote_tip}, None


def _gitlink(clone: Path, host_tip: str, path: str) -> Tuple[Optional[str], Optional[str]]:
    rc, out, err = _git(clone, "ls-tree", host_tip, "--", path)
    rows = [line.split() for line in out.splitlines() if line.strip()]
    if (rc != 0 or len(rows) != 1 or len(rows[0]) < 4
            or rows[0][0:2] != ["160000", "commit"]
            or not OID.fullmatch(rows[0][2])):
        detail = " ".join((err or out).split())
        return None, (f"{host_tip[:12]}:{path} is not exactly one gitlink"
                      + (f": {_tail(detail)}" if detail else ""))
    return rows[0][2].lower(), None


def _submodules(clone: Path, host_tip: str, host: str) -> Tuple[Optional[List[dict]], Optional[str]]:
    rc, out, err = _git(clone, "show", f"{host_tip}:.gitmodules")
    if rc != 0:
        # ``git submodule update --recursive`` is routinely used for a graph
        # whose leaves have no .gitmodules of their own.  A proven absent blob
        # is therefore an exact empty edge set; every other git failure remains
        # unmeasured.
        crc, _, _ = _git(clone, "cat-file", "-e", f"{host_tip}^{{commit}}")
        if crc == 0 and ("does not exist" in (err or out)
                         or "exists on disk, but not" in (err or out)):
            return [], None
        return None, (f"{host}@{host_tip[:12]} is delivered with submodules, "
                      f"but .gitmodules could not be read: "
                      f"{_tail(err or out)}")
    try:
        parsed = discover_forks.parse_gitmodules(out)
    except Exception as exc:
        return None, f"could not parse {host}@{host_tip[:12]}:.gitmodules: {exc}"
    rows = []
    for submodule in parsed:
        slug = discover_forks.submodule_repo(
            str(submodule.get("url") or ""), f"vibeic/{host}")
        if not slug or slug.split("/", 1)[0].lower() != "vibeic":
            continue
        rows.append({
            "repo": slug.split("/", 1)[1],
            "path": submodule["path"],
            "url": submodule["url"],
        })
    return rows, None


def _edge_scan(eda_root: Path, forks_root: Path, registry: Dict[str, dict],
               max_depth: int) -> Tuple[List[dict], List[dict]]:
    direct, why = _direct_recursive_hosts(eda_root)
    if direct is None:
        return [], [{"state": "UNMEASURED", "detail": why}]
    queue = [(row["repo"], row["recursive"], 0, row["source"])
             for row in direct.values()]
    seen = set()
    edges: List[dict] = []
    findings: List[dict] = []
    while queue:
        host, recursive, depth, source = queue.pop(0)
        identity = (host.lower(), depth)
        if identity in seen:
            continue
        seen.add(identity)
        declared = registry.get(host.lower())
        if declared is None:
            findings.append({"host": host, "state": "UNMEASURED",
                             "detail": f"delivered host {host} is absent from FORKS.json"})
            continue
        observed, why = _observe_branch(forks_root, host, declared["branch"])
        if observed is None:
            findings.append({"host": host, "state": "UNMEASURED", "detail": why})
            continue
        subs, why = _submodules(observed["clone"], observed["tip"], host)
        if subs is None:
            findings.append({"host": host, "state": "UNMEASURED", "detail": why})
            continue
        for submodule in subs:
            child = submodule["repo"]
            child_decl = registry.get(child.lower())
            if child_decl is None:
                findings.append({
                    "host": host, "repo": child, "path": submodule["path"],
                    "state": "UNMEASURED",
                    "detail": (f"vibeic/{child} is shipped as a submodule but is "
                               "not declared in FORKS.json"),
                })
                continue
            child_observed, why = _observe_branch(
                forks_root, child, child_decl["branch"])
            if child_observed is None:
                findings.append({
                    "host": host, "repo": child, "path": submodule["path"],
                    "state": "UNMEASURED", "detail": why,
                })
                continue
            current, why = _gitlink(
                observed["clone"], observed["tip"], submodule["path"])
            if current is None:
                findings.append({
                    "host": host, "repo": child, "path": submodule["path"],
                    "state": "UNMEASURED", "detail": why,
                })
                continue
            ancestor_rc, ancestor_out, ancestor_err = _git(
                child_observed["clone"], "merge-base", "--is-ancestor",
                current, child_observed["tip"],
            )
            if current == child_observed["tip"]:
                relation = "current"
            elif ancestor_rc == 0:
                relation = "advance"
            elif ancestor_rc == 1:
                relation = "diverged"
            else:
                relation = "unmeasured"
            edge = {
                "host": host,
                "host_branch": observed["branch"],
                "host_tip": observed["tip"],
                "repo": child,
                "branch": child_observed["branch"],
                "path": submodule["path"],
                "url": submodule["url"],
                "from": current,
                "to": child_observed["tip"],
                "depth": depth,
                "source": source,
                "relation": relation,
            }
            edges.append(edge)
            if relation == "diverged":
                findings.append({**edge, "state": "NEEDS_HUMAN",
                                 "detail": (f"{child} published branch no longer "
                                            f"descends from gitlink {current[:12]}")})
            elif relation == "unmeasured":
                diagnostic = " ".join((ancestor_err or ancestor_out).split())
                findings.append({**edge, "state": "UNMEASURED",
                                 "detail": (f"could not prove {current[:12]} is an "
                                            f"ancestor of {child_observed['tip'][:12]}"
                                            + (f": {_tail(diagnostic)}" if diagnostic else ""))})
            if recursive and depth + 1 < max_depth:
                queue.append((child, True, depth + 1,
                              f"{host}:{submodule['path']}"))
    return edges, findings


def _update_host(forks_root: Path, registry: Dict[str, dict], host: str,
                 edges: List[dict], dry_run: bool) -> dict:
    first = edges[0]
    clone = _clone_path(forks_root, host)
    observed_tip = first["host_tip"]
    branch = first["host_branch"]
    row = {
        "host": host,
        "branch": branch,
        "from": observed_tip,
        "to": None,
        "state": "?",
        "updates": [{k: edge[k] for k in ("repo", "branch", "path", "from", "to")}
                    for edge in edges],
        "checks": [],
    }
    if any(edge["host_tip"] != observed_tip or edge["host_branch"] != branch
           for edge in edges):
        row.update(state="UNMEASURED",
                   detail="one host resolved to contradictory branch observations")
        return row
    if dry_run:
        row.update(state="WOULD_UPDATE",
                   detail=f"would advance {len(edges)} vendored gitlink(s)")
        return row

    before = _git(clone, "status", "--porcelain")[1]
    tmp = Path(tempfile.mkdtemp(prefix=f"vendored_closure_{host}_"))
    wt = tmp / "wt"
    try:
        rc, _, err = _git(clone, "worktree", "add", "--detach", str(wt), observed_tip,
                          timeout=1800)
        if rc != 0:
            row.update(state="UNMEASURED",
                       detail=f"could not create host worktree: {_tail(err)}")
            return row
        for edge in edges:
            rc, _, err = _git(
                wt, "update-index", "--add", "--cacheinfo",
                f"160000,{edge['to']},{edge['path']}",
            )
            if rc != 0:
                row.update(state="UNMEASURED",
                           detail=(f"could not stage {edge['path']} at "
                                   f"{edge['to'][:12]}: {_tail(err)}"))
                return row
        rc, out, err = _git(wt, "diff", "--cached", "--name-only")
        changed = [line for line in out.splitlines() if line.strip()]
        expected = sorted(edge["path"] for edge in edges)
        if rc != 0 or sorted(changed) != expected:
            row.update(state="UNMEASURED",
                       detail=(f"candidate index changed {sorted(changed)}, expected "
                               f"{expected}: {_tail(err)}"))
            return row
        for edge in edges:
            child_clone = _clone_path(forks_root, edge["repo"])
            url_rc, child_url, _ = _git(child_clone, "remote", "get-url", "origin")
            materialise_args = []
            edge_url = str(edge.get("url") or "")
            # THE TWO -c OPTIONS BELOW CAN FIGHT EACH OTHER. MEASURED
            # 2026-09-09, and it cost a round.
            #
            # `submodule.<name>.url` is set to the child clone's origin, and
            # `url.<that>.insteadOf=<edge url>` is a PREFIX rule. When the
            # edge URL is a prefix of the child origin -- the ordinary `.git`
            # suffix case -- the rule matches the URL this function itself
            # just set, replaces that prefix with the whole child origin, and
            # produces the child origin with the suffix twice.
            #
            # `vibeic/yosys` vendors `sv-elab`, and the two disagree by
            # exactly that suffix:
            #     .gitmodules           https://github.com/vibeic/sv-elab
            #     child clone origin    https://github.com/vibeic/sv-elab.git
            # so materialisation cloned
            #     https://github.com/vibeic/sv-elab.git.git
            #     remote: Repository not found.
            # The closure went UNMEASURED, `run_tick` returned 6, `build_page`
            # was skipped, and the public page missed a day.
            #
            # `child_url.startswith(edge_url)` is exactly the colliding case,
            # and it is also exactly the case that needs no rule: a
            # suffix-extended URL names the same remote, which the
            # `submodule.<name>.url` set already points at directly. A real
            # mirror (a local path, another host) is not a prefix extension,
            # so it still gets its rule.
            #
            # ONLY the insteadOf is dropped. The per-submodule url answers a
            # different failure -- a worktree inheriting a local path from the
            # shared .git/config, which cost three rounds on 2026-09-05 --
            # and gating it here would trade one round-losing bug for the
            # other.
            self_applying = bool(edge_url) and child_url.strip().startswith(edge_url)
            if (url_rc == 0 and child_url.strip()
                    and not edge_url.startswith(("./", "../"))):
                # The already-fetched child clone names the exact remote whose
                # branch tip was verified above.  Reuse that URL for candidate
                # materialisation; in production it is normally identical,
                # while hermetic/local mirrors need an explicit insteadOf.
                if not self_applying:
                    materialise_args.extend([
                        "-c", f"url.{child_url.strip()}.insteadOf={edge_url}",
                    ])
                # ...AND NAME IT DIRECTLY. MEASURED 2026-09-05: three rounds
                # refused to publish on
                #     fatal: transport 'file' not allowed
                #     clone of '/home/reyerchu/vibe-ic-forks/OpenROAD/src/sta'
                #             into submodule path '<worktree>/src/sta' failed
                # The host CLONE's own .git/config carries
                # `submodule.module/OpenSTA.url = <a local absolute path>` --
                # left there by whoever first materialised it -- and
                # `git worktree add` SHARES that config, so the worktree
                # inherited the local path. `.git/config` beats `.gitmodules`,
                # so the insteadOf above rewrote a URL that was never consulted,
                # and git >= 2.38 refuses the `file` transport for submodules
                # (CVE-2022-39253). Setting the per-submodule key for this
                # invocation overrides the inherited one without writing to the
                # shared clone.
                # The config key is the submodule NAME, which is not the path
                # (here: `module/OpenSTA` for path `src/sta`), so ask the tree
                # being materialised for it rather than guessing.
                nrc, nout, _ = _git(
                    wt, "config", "-f", ".gitmodules", "--get-regexp",
                    r"^submodule\..*\.path$",
                )
                sub_name = None
                if nrc == 0:
                    for line in nout.splitlines():
                        key, _, value = line.partition(" ")
                        if value.strip() == edge["path"]:
                            sub_name = key[len("submodule."):-len(".path")]
                            break
                if sub_name:
                    materialise_args.extend([
                        "-c", f"submodule.{sub_name}.url={child_url.strip()}",
                    ])
            rc, _, err = _git(
                wt, *materialise_args, "submodule", "update", "--init",
                "--recursive", "--", edge["path"], timeout=1800,
            )
            if rc != 0:
                row.update(state="UNMEASURED",
                           detail=(f"could not materialise {edge['path']} at "
                                   f"{edge['to'][:12]}: {_tail(err)}"))
                return row

        checks = registry[host.lower()].get("post_merge_check") or []
        if not isinstance(checks, list):
            checks = [{"_malformed": checks}]
        row["checks"] = daily_merge.run_post_merge_checks(wt, checks)
        failed = [check for check in row["checks"] if not check.get("ok")]
        if failed:
            natural = any(check.get("outcome") == "natural" for check in failed)
            row.update(
                state="CHECK_FAILED" if natural else "UNMEASURED",
                detail=(f"{len(failed)} of {len(row['checks'])} host check(s) "
                        "did not pass; nothing was pushed: "
                        + "; ".join(f"{x.get('name')} rc={x.get('rc')}"
                                    for x in failed)[:240]),
            )
            return row

        names = ", ".join(edge["repo"] for edge in edges)
        subject = (f"chore(deps): advance vendored {names}"
                   if len(edges) <= 2 else
                   "chore(deps): advance vendored fork dependencies")
        rc, _, err = _run([
            "git", "-C", str(wt),
            "-c", "user.name=vibeic-fork-gatekeeper",
            "-c", "user.email=fork-gatekeeper@vibeic.ai",
            "commit", "-s", "-m", subject,
        ], timeout=300)
        if rc != 0:
            row.update(state="UNMEASURED",
                       detail=f"could not commit host gitlinks: {_tail(err)}")
            return row
        rc, out, err = _git(wt, "rev-parse", "HEAD^{commit}")
        candidate = out.strip().lower()
        if rc != 0 or not OID.fullmatch(candidate):
            row.update(state="UNMEASURED",
                       detail=f"candidate commit did not resolve: {_tail(err)}")
            return row
        row["to"] = candidate
        rc, out, err = _git(wt, "merge-base", "--is-ancestor", observed_tip, candidate)
        if rc != 0:
            row.update(state="NEEDS_HUMAN" if rc == 1 else "UNMEASURED",
                       detail=(f"candidate does not prove a fast-forward from "
                               f"{observed_tip[:12]}: {_tail(err or out)}"))
            return row
        rc, push_out, push_err = _git(
            wt, "push", "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{observed_tip}",
            "origin", f"HEAD:refs/heads/{branch}", timeout=1800,
        )
        if rc != 0:
            detail = " ".join((push_out + " " + push_err).split())
            row.update(state="NEEDS_HUMAN" if "rejected" in detail.lower()
                       else "UNMEASURED",
                       detail=(f"CAS push of {host}/{branch} was not confirmed: "
                               f"{_tail(detail)}"))
            return row
        rc, out, err = _git(clone, "ls-remote", "--refs", "origin",
                            f"refs/heads/{branch}")
        readback = [line.split()[0].lower() for line in out.splitlines()
                    if line.strip() and line.split()]
        if rc != 0 or readback != [candidate]:
            row.update(state="UNMEASURED",
                       detail=(f"push returned zero but remote readback was "
                               f"{readback or 'missing'}, expected {candidate}"))
            return row
        row.update(state="UPDATED", detail=f"advanced {len(edges)} vendored gitlink(s)")
        return row
    finally:
        _git(clone, "worktree", "remove", "--force", str(wt), timeout=1800)
        shutil.rmtree(tmp, ignore_errors=True)
        after = _git(clone, "status", "--porcelain")[1]
        if after != before:
            row["shared_checkout_changed"] = True
            row["state"] = "UNMEASURED"
            row["detail"] = (row.get("detail", "")
                             + "; shared checkout changed during closure")


def run(eda_root: Path, forks_root: Path, forks_json: Path, *, dry_run: bool,
        max_depth: int, max_passes: int) -> Tuple[int, dict]:
    registry, why = _registry(forks_json)
    result = {
        "program": PROGRAM,
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "dry_run": dry_run,
        "passes": [],
        "updated_hosts": [],
        "source_errors": [],
    }
    if registry is None:
        result.update(status="unmeasured", source_errors=[why])
        return RC_UNMEASURED, result

    updated_hosts = set()
    for pass_number in range(1, max_passes + 1):
        edges, findings = _edge_scan(eda_root, forks_root, registry, max_depth)
        pass_row = {"pass": pass_number, "edges": edges,
                    "findings": findings, "updates": []}
        result["passes"].append(pass_row)
        if findings:
            needs_human = any(row.get("state") == "NEEDS_HUMAN"
                              for row in findings)
            result.update(status="needs_human" if needs_human else "unmeasured")
            return (RC_NEEDS_HUMAN if needs_human else RC_UNMEASURED), result
        stale = [edge for edge in edges if edge["relation"] == "advance"]
        if not stale:
            result["updated_hosts"] = sorted(updated_hosts)
            result["status"] = "dry_run" if dry_run else "complete"
            return RC_OK, result
        grouped: Dict[str, List[dict]] = {}
        for edge in stale:
            grouped.setdefault(edge["host"], []).append(edge)
        if dry_run:
            for host, host_edges in sorted(grouped.items()):
                pass_row["updates"].append(
                    _update_host(forks_root, registry, host, host_edges, True))
            result["updated_hosts"] = sorted(grouped)
            result["status"] = "dry_run"
            return RC_OK, result
        # Children first reduces the number of fixed-point passes for nested
        # delivery chains.  The next scan still re-observes every remote tip.
        ordered = sorted(grouped.items(),
                         key=lambda item: max(e["depth"] for e in item[1]),
                         reverse=True)
        for host, host_edges in ordered:
            update = _update_host(forks_root, registry, host, host_edges, False)
            pass_row["updates"].append(update)
            if update["state"] != "UPDATED":
                result["updated_hosts"] = sorted(updated_hosts)
                result["status"] = ("needs_human" if update["state"] in {
                    "NEEDS_HUMAN", "CHECK_FAILED"} else "unmeasured")
                return (RC_NEEDS_HUMAN if result["status"] == "needs_human"
                        else RC_UNMEASURED), result
            updated_hosts.add(host)
    result["updated_hosts"] = sorted(updated_hosts)
    result["status"] = "unmeasured"
    result["source_errors"] = [
        f"vendored dependency graph did not reach a fixed point in {max_passes} passes"
    ]
    return RC_UNMEASURED, result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eda-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--forks-root", type=Path,
                        default=Path(os.environ.get(
                            "VIBEIC_FORKS_ROOT", "/home/reyerchu/vibe-ic-forks")))
    parser.add_argument("--forks-json", type=Path,
                        default=Path(__file__).resolve().with_name("FORKS.json"))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-passes", type=int, default=5)
    args = parser.parse_args(argv)
    if args.max_depth < 1 or args.max_passes < 1:
        parser.error("--max-depth and --max-passes must be positive")
    rc, result = run(
        args.eda_root.resolve(), args.forks_root.resolve(),
        args.forks_json.resolve(), dry_run=args.dry_run,
        max_depth=args.max_depth, max_passes=args.max_passes,
    )
    if args.json:
        _atomic_json(args.json, result)
    stale = sum(1 for p in result["passes"] for e in p.get("edges", [])
                if e.get("relation") == "advance")
    print(f"vendored dependency closure: {result['status']} — "
          f"{len(result.get('updated_hosts', []))} host(s) updated, "
          f"{stale} stale edge observation(s)")
    # The detail is printed WHOLE. Truncating here is what made the earlier
    # `_tail` fix invisible: a complete diagnosis assembled upstream was cut to
    # 120 characters on the way out, so the log, the .txt sidecar and anything
    # grepping them all still showed only "Cloning into '/tmp/...'". A line that
    # is long is readable; a line that stops before the reason is not.
    for pass_row in result["passes"]:
        for update in pass_row.get("updates", []):
            print(f"  {update['host']:<16} {update['state']:<14} "
                  f"{update.get('detail', '')}")
        for finding in pass_row.get("findings", []):
            print(f"  {finding.get('host', '?'):<16} "
                  f"{finding.get('state', 'UNMEASURED'):<14} "
                  f"{finding.get('detail', '')}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
