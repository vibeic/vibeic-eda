#!/usr/bin/env python3
"""Make the image state BOTH halves of what its PDK volume is: where it came
from, and every byte this build changed in it.

WHY
---
The open_pdks statement in the Dockerfile is an ASSERTION about a PREBUILT ciel
volume (vibeic-eda#74/#78/#79). It is enforced by resolving the symlink:

    /foss/pdks/sky130A -> /foss/pdks/ciel/sky130/versions/<sha>/sky130A

and refusing to build unless the resolved PATH carries the declared sha. That
check is exactly right about PROVENANCE and structurally blind to CONTENT: a
step that rewrites a file INSIDE that tree leaves the path untouched, so the
assertion still passes while the sentence it stands for -- "this is what
open_pdks <sha> produced" -- has quietly stopped being true. The volume is now
"open_pdks <sha>, plus N local edits", and nothing in the image says so.

That is the same defect class #79 exists to eliminate, arriving from the other
direction: not a claim that was advanced past its evidence, but evidence that
moved out from under a claim nobody advanced.

WHAT THIS DOES
--------------
Two build steps around every local PDK modification:

    baseline   before any local step touches the volume: record sha256 for
               every file the base image DELIVERED.
    verify     after the last one: re-digest, diff against the baseline, and
               require every difference to be COVERED by a declared entry in
               the manifest. Then write the record the image ships.

The load-bearing property is that the manifest is checked against a MEASURED
diff rather than trusted. A future patch step that nobody declares changes
files that no entry covers, and the build FAILS naming them. Declaring is not
something a person has to remember; it is the only way to get a green build.

The counts cut the other way too. An entry that matches FEWER files than it
declares is a step that has silently stopped applying; an entry that matches
MORE is a step whose blast radius grew. Both fail. A declaration cannot outlive
the thing it describes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- digesting


def digest_file(path: Path) -> str:
    """sha256 of a regular file; a symlink digests as its TARGET TEXT.

    Retargeting a symlink changes nothing about any file's bytes, so hashing
    only regular-file content would let it through. The volume is reached
    through symlinks, so that is not a hypothetical hole.
    """
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_tree(root: Path) -> dict:
    """{relative posix path: digest} for every file and symlink under root.

    Directories carry no bytes and are skipped. A symlink TO a directory is
    listed by os.walk under dirnames and is not descended into
    (followlinks=False), so it is digested here as the link it is.
    """
    out = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        dirnames.sort()
        candidates = [n for n in dirnames if (d / n).is_symlink()] + filenames
        for name in sorted(candidates):
            p = d / name
            rel = p.relative_to(root).as_posix()
            try:
                out[rel] = digest_file(p)
            except OSError as exc:               # unreadable is not "absent"
                out[rel] = f"unreadable:{exc.errno}"
    return out


def write_baseline(digests: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{v}  {k}\n" for k, v in sorted(digests.items())))


def read_baseline(path: Path) -> dict:
    got = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        got[rel] = digest
    return got


def diff_trees(base: dict, cur: dict) -> dict:
    """-> {'modified': {rel: (old, new)}, 'added': [rel], 'removed': [rel]}"""
    return {
        "modified": {k: (base[k], cur[k])
                     for k in sorted(set(base) & set(cur)) if base[k] != cur[k]},
        "added": sorted(set(cur) - set(base)),
        "removed": sorted(set(base) - set(cur)),
    }


# ---------------------------------------------------------------- manifest


def glob_to_re(pattern: str) -> re.Pattern:
    """`*` = one path segment, `**` = any depth, `?` = one non-separator char.

    fnmatch is not used on purpose: its `*` crosses `/`, so a glob meant to
    name one directory's files would silently cover the whole subtree, which
    is the opposite of what a scope declaration is for.
    """
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def load_manifest(path: Path) -> list:
    doc = json.loads(Path(path).read_text())
    mods = doc.get("modifications")
    if not isinstance(mods, list):
        raise ValueError(f"{path}: 'modifications' must be a list")
    for m in mods:
        for key in ("id", "issue", "what", "paths"):
            if not m.get(key):
                raise ValueError(f"{path}: entry missing '{key}': {m!r}")
        m["_res"] = [glob_to_re(p) for p in m["paths"]]
    ids = [m["id"] for m in mods]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate modification id")
    return mods


def reconcile(delta: dict, mods: list) -> tuple:
    """Match every measured difference to exactly one declared entry.

    -> (problems: [str], per_entry: {id: {'modified': [...], ...}})
    """
    problems = []
    per = {m["id"]: {"modified": [], "added": [], "removed": []} for m in mods}

    for kind, paths in (("modified", list(delta["modified"])),
                        ("added", delta["added"]),
                        ("removed", delta["removed"])):
        for rel in paths:
            owners = [m for m in mods if any(r.match(rel) for r in m["_res"])]
            if not owners:
                problems.append(
                    f"UNDECLARED {kind}: {rel}\n"
                    f"    No entry in the local-modification manifest covers "
                    f"this path. Either a build step is changing the PDK "
                    f"volume without declaring it, or the base image no longer "
                    f"delivers what this build was written against.")
                continue
            if len(owners) > 1:
                problems.append(
                    f"AMBIGUOUS {kind}: {rel} is claimed by "
                    f"{', '.join(m['id'] for m in owners)}. Declarations must "
                    f"partition the volume, or a count can be satisfied twice.")
                continue
            per[owners[0]["id"]][kind].append(rel)

    for m in mods:
        got = per[m["id"]]
        for kind in ("modified", "added", "removed"):
            want = m.get(f"expect_{kind}")
            if want is None:
                continue
            if len(got[kind]) != want:
                why = ("A step that has stopped applying satisfies every "
                       "post-condition it no longer reaches."
                       if len(got[kind]) < want else
                       "A step whose blast radius grew is a different change "
                       "than the one that was reviewed.")
                problems.append(
                    f"COUNT {m['id']}: declares expect_{kind}={want}, measured "
                    f"{len(got[kind])}.\n"
                    f"    {why}\n"
                    f"    measured: {got[kind] or '(none)'}")
    return problems, per


# ---------------------------------------------------------------- provenance


def check_symlinks(links, sha: str) -> list:
    """The #79 path assertion, re-run at the END of the build.

    The original runs before the local steps; re-running it last means a step
    that re-points the symlink AFTER it cannot ship either.
    """
    bad = []
    for link in links:
        tgt = os.path.realpath(link)
        if sha not in tgt:
            bad.append(
                f"PROVENANCE: {link} resolves to '{tgt}', which does not carry "
                f"the declared open_pdks volume-contents sha {sha}.")
    return bad


def build_record(sha: str, root: str, mods: list, delta: dict, per: dict,
                 total: int) -> dict:
    changed = sum(len(v["modified"]) + len(v["added"]) + len(v["removed"])
                  for v in per.values())
    return {
        "what_this_is": (
            "The PDK volume this image ships, stated in full: the upstream "
            "artefact it was delivered as, plus every modification this build "
            "made to it. Neither half is complete on its own."),
        "upstream": {
            "open_pdks": sha,
            "delivered_as": "a prebuilt ciel volume baked into the base image",
            "built_here": False,
        },
        "root": root,
        "files_total": total,
        "files_modified_locally": changed,
        "local_modifications": [
            {
                "id": m["id"],
                "issue": m["issue"],
                "what": m["what"],
                "step": m.get("step", ""),
                "modified": [
                    {"path": rel,
                     "upstream_sha256": delta["modified"][rel][0],
                     "shipped_sha256": delta["modified"][rel][1]}
                    for rel in per[m["id"]]["modified"]
                ],
                "added": per[m["id"]]["added"],
                "removed": per[m["id"]]["removed"],
            }
            for m in mods
        ],
        "how_this_is_enforced": (
            "Every file was digested before the first local step and again "
            "after the last. Any difference not covered by an entry above "
            "fails the build, so this list cannot be incomplete."),
    }


# ---------------------------------------------------------------- cli


def cmd_baseline(args) -> int:
    root = Path(args.root)
    if not root.is_dir():
        print(f"FAIL: {root} is not a directory. The baseline would be empty "
              f"and every later check would pass by comparing nothing.",
              file=sys.stderr)
        return 1
    digests = digest_tree(root)
    if not digests:
        print(f"FAIL: {root} contains no files.", file=sys.stderr)
        return 1
    write_baseline(digests, Path(args.out))
    print(f"pdk baseline: {len(digests)} file(s) under {root} digested "
          f"as delivered -> {args.out}")
    return 0


def cmd_verify(args) -> int:
    if not args.upstream_sha:
        print("FAIL: --upstream-sha is empty. An ARG declared before the "
              "first FROM expands to the empty string inside a stage unless "
              "redeclared, and this check would then record a PDK it cannot "
              "name (vibeic-eda#60).", file=sys.stderr)
        return 1

    base_path = Path(args.baseline)
    if not base_path.is_file():
        print(f"FAIL: no baseline at {base_path}. The baseline step did not "
              f"run, so nothing knows what the volume looked like on arrival.",
              file=sys.stderr)
        return 1

    root = Path(args.root)
    base = read_baseline(base_path)
    cur = digest_tree(root)
    delta = diff_trees(base, cur)

    try:
        mods = load_manifest(Path(args.manifest))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    problems = check_symlinks(args.pdk_symlink or [], args.upstream_sha)
    p2, per = reconcile(delta, mods)
    problems += p2

    if problems:
        print("FAIL: the PDK volume this image would ship is not the volume it "
              "would describe.", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nDeclare it in tools/pdk/local_mods.json, or stop changing it.",
              file=sys.stderr)
        return 1

    record = build_record(args.upstream_sha, str(root), mods, delta, per,
                          len(cur))
    out = Path(args.record)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")

    print(f"PDK self-description ({out}):")
    print(f"  upstream        : open_pdks {args.upstream_sha} "
          f"(prebuilt ciel volume, not built here)")
    print(f"  files digested  : {len(cur)}")
    if not record["local_modifications"]:
        print("  local changes   : none")
    for m in record["local_modifications"]:
        n = len(m["modified"]) + len(m["added"]) + len(m["removed"])
        print(f"  local change    : {m['id']} ({m['issue']}) — {n} file(s)")
        print(f"                    {m['what']}")
    print(f"  files modified  : {record['files_modified_locally']} of "
          f"{len(cur)}, all declared")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="digest the volume as delivered")
    b.add_argument("--root", required=True)
    b.add_argument("--out", required=True)
    b.set_defaults(fn=cmd_baseline)

    v = sub.add_parser("verify", help="reconcile the volume against the "
                                      "declared local modifications")
    v.add_argument("--root", required=True)
    v.add_argument("--baseline", required=True)
    v.add_argument("--manifest", required=True)
    v.add_argument("--upstream-sha", default="")
    v.add_argument("--record", required=True)
    v.add_argument("--pdk-symlink", action="append", default=[],
                   help="repeatable; each must still resolve to a path "
                        "carrying --upstream-sha")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
