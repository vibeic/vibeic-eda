"""Every RUN that writes under /foss/pdks runs as root.

/foss/pdks is root-owned in the base image (drwxr-xr-x root root), and the
image switches to `USER 1000` for its delivery checks so those are verified in
the identity a user has. A PDK write placed in one of those RUNs cannot work,
and nothing in the source says so until `bake eda` reaches it.

MEASURED (vibeic-eda#240). The IHP Xyce plugin loop was added to the `USER 1000`
delivery-check RUN, the one that proves `buildxyceplugin` can build a plugin.
Its first write, `mkdir -p /foss/pdks/ihp-sg13g2/libs.tech/xyce/plugins`, fails
as uid 1000 in 0.3.79 with "Permission denied", so the image did not build. The
PR's own evidence ran the same body as root through `docker exec`, which is why
it looked fine there. The loop now lives in the `USER root` PDK-corrections RUN.

WHAT COUNTS AS A WRITE. This reads the RUN text: the paths a shell command
writes (mkdir/rm/touch/chmod/tee, the destination of cp/mv/install/ln, sed -i,
patch -d, find -delete/-exec rm, a `>`/`>>` target, an `-o/--out/--record`
value), after expanding variables the RUN assigns a /foss/pdks value and
resolving relative paths against a `cd` into /foss/pdks. A path that is only
READ (cp's source, --root, Xyce -plugin, readlink) is not a write, so a
uid-1000 RUN may still read the PDK. Comment lines are dropped first, as Docker
drops them, so prose cannot change the verdict.

WHAT IT DOES NOT SEE. A program that writes under /foss/pdks on its own, with
no /foss/pdks path in the RUN text (pdk_ihp_pycell_runtime_install.py
--pdk ihp-sg13g2, refresh_versions_manifest.sh), is invisible here. Every such
call is in a root RUN today; this test does not prove that it stays so.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
PDKS = "/foss/pdks"

_ROOT_USERS = {"root", "0"}
_RUN_FLAG = re.compile(r"^--[a-z-]+=\S+$")
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", ";;"}
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while",
             "until", "!", "time"}
_REDIRECT = re.compile(r"^\d*(>>?|>\||&>>?)$")
_ALL_ARGS = {"mkdir", "touch", "rm", "rmdir", "chmod", "chown", "chgrp",
             "truncate", "unlink", "shred", "tee"}
_DEST_LAST = {"cp", "mv", "install", "ln", "rsync"}
_OUT_OPTS = {"-o", "--out", "--output", "--record", "--dest", "--destination"}
_DEST_OPTS = {"-t", "--target-directory"}
# Commands whose `-o` is not an output (ls -o, grep -o): only a redirection
# on their line can write.
_READERS = {"ls", "grep", "egrep", "readlink", "realpath", "cat", "head",
            "tail", "stat", "test", "[", "file", "du", "wc", "echo", "printf",
            "diff", "cmp", "sha256sum", "md5sum", "awk", "basename", "dirname"}


class Unparseable(ValueError):
    """A RUN that names /foss/pdks and cannot be tokenised: never a pass."""


def instructions(text: str) -> list[tuple[int, str, str]]:
    """(first line, OPCODE, body) with continuations joined and comments dropped.

    Docker drops a line whose first non-blank character is `#` anywhere,
    including inside a backslash continuation, so this does the same.
    """
    out: list[tuple[int, str, str]] = []
    pending, start = "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not pending:
            start = n
        pending = f"{pending} {s}".strip() if pending else s
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        op, _, body = pending.partition(" ")
        out.append((start, op.upper(), body.strip()))
        pending = ""
    if pending:
        raise Unparseable("Dockerfile ends inside a backslash continuation")
    return out


def _is_root(user: str | None) -> bool:
    if user is None:          # the stage's image default: not known to be root
        return False
    return user.split(":", 1)[0] in _ROOT_USERS


def _shell_of(body: str) -> str:
    """The shell text of a RUN: flags stripped, exec form unwrapped."""
    words = body.split(" ")
    while words and _RUN_FLAG.match(words[0]):
        words.pop(0)
    body = " ".join(words).strip()
    if body.startswith("["):
        argv = json.loads(body)
        for i, a in enumerate(argv[:-1]):
            if a in ("-c", "-lc", "-ec", "-euc"):
                return argv[i + 1]
        return " ".join(argv)
    return body


def _expand(text: str, env: dict[str, str]) -> str:
    for name, value in env.items():
        text = text.replace("${" + name + "}", value)
        text = re.sub(r"\$" + name + r"(?![A-Za-z0-9_])",
                      lambda _m, v=value: v, text)
    return text


def _commands(shell: str) -> list[list[str]]:
    lex = shlex.shlex(shell, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""        # comment LINES are gone; `${m#*:}` is not one
    try:
        tokens = list(lex)
    except ValueError as exc:
        raise Unparseable(f"{exc}: {shell[:120]!r}") from exc
    cmds: list[list[str]] = [[]]
    for t in tokens:
        if t in _SEPARATORS or (set(t) <= set(";&|()") and t):
            cmds.append([])
        else:
            cmds[-1].append(t)
    return [c for c in cmds if c]


def _resolve(path: str, cwd: str | None) -> str | None:
    if path.startswith(("$", "~")):     # a variable this RUN never set to a PDK
        return None
    if path.startswith("/"):
        return posixpath.normpath(path)
    if cwd and not path.startswith("-"):
        return posixpath.normpath(posixpath.join(cwd, path))
    return None


def _under_pdks(path: str | None) -> bool:
    return bool(path) and (path == PDKS or path.startswith(PDKS + "/"))


def pdk_writes(shell: str) -> list[str]:
    """Every write under /foss/pdks the shell text names, as `cmd path`."""
    env: dict[str, str] = {}
    for _ in range(4):                 # X=/foss/pdks/a; Y=$X/b -> Y resolved
        for m in re.finditer(r"(?:^|[\s;&|(])([A-Za-z_][A-Za-z0-9_]*)="
                             r"[\"']?(\$\{?[A-Za-z_][A-Za-z0-9_]*\}?[^\s;\"']*"
                             r"|/foss/pdks[^\s;\"']*)", shell):
            value = _expand(m.group(2), env)
            if _under_pdks(value):
                env[m.group(1)] = value
    shell = _expand(shell, env)
    if PDKS not in shell:
        return []
    found: list[str] = []
    cwd: str | None = None
    for words in _commands(shell):
        while words and (words[0] in _KEYWORDS or _ASSIGN.match(words[0])):
            words = words[1:]
        if not words:
            continue
        cmd, rest = posixpath.basename(words[0]), words[1:]
        if cmd == "for":                       # `for X in a b c` writes nothing
            continue
        if cmd in ("sh", "bash") and any(a in ("-c", "-lc", "-ec") for a in rest):
            i = next(i for i, a in enumerate(rest) if a in ("-c", "-lc", "-ec"))
            if i + 1 < len(rest):
                found += pdk_writes(rest[i + 1])
            continue
        # redirections, wherever they sit on the line
        args: list[str] = []
        i = 0
        while i < len(rest):
            t = rest[i]
            if _REDIRECT.match(t) and i + 1 < len(rest):
                tgt = _resolve(rest[i + 1], cwd)
                if _under_pdks(tgt):
                    found.append(f"{cmd} {t} {tgt}")
                i += 2
                continue
            if t in (">&", "<", "<<", "<<<", "<&") or t.endswith(">&"):
                i += 2
                continue
            args.append(t)
            i += 1
        if cmd == "cd":
            cwd = _resolve(args[0], cwd) if args else None
            continue
        targets: list[str] = []
        positional = [a for a in args if not a.startswith("-")]
        opts = (_DEST_OPTS if cmd in _DEST_LAST          # install -o is OWNER
                else set() if cmd in _READERS else _OUT_OPTS)
        for j, a in enumerate(args):
            if a in opts and j + 1 < len(args):
                targets.append(args[j + 1])
            elif "=" in a and a.split("=", 1)[0] in opts:
                targets.append(a.split("=", 1)[1])
            elif cmd == "dd" and a.startswith("of="):
                targets.append(a[3:])
        if cmd in _ALL_ARGS:
            targets += positional
        elif cmd in _DEST_LAST:
            if "-d" in args and cmd == "install":
                targets += positional
            elif len(positional) >= 2:
                targets.append(positional[-1])
            elif positional and cwd:
                targets.append(cwd)
        elif cmd == "sed" and any(a == "-i" or a.startswith("-i") or
                                  a == "--in-place" for a in args):
            targets += positional
        elif cmd == "patch":
            for j, a in enumerate(args):
                if a == "-d" and j + 1 < len(args):
                    targets.append(args[j + 1])
            targets += positional
        elif cmd == "find" and ("-delete" in args or any(
                a in ("-exec", "-execdir") and j + 1 < len(args)
                and posixpath.basename(args[j + 1]) in ("rm", "mv", "sed")
                for j, a in enumerate(args))):
            for a in args:
                if a.startswith(("-", "(", "!")):
                    break
                targets.append(a)
        for t in targets:
            p = _resolve(t, cwd)
            if _under_pdks(p):
                found.append(f"{cmd} {p}")
    return found


def audit(text: str) -> tuple[list[str], list[tuple[int, str | None, list[str]]]]:
    """(violations, every RUN that writes under /foss/pdks with its user)."""
    user: str | None = None
    writers: list[tuple[int, str | None, list[str]]] = []
    bad: list[str] = []
    for line, op, body in instructions(text):
        if op == "FROM":
            user = None
        elif op == "USER":
            user = body.split()[0]
        elif op == "RUN":
            shell = _shell_of(body)
            if PDKS not in shell:
                continue
            w = pdk_writes(shell)
            if not w:
                continue
            writers.append((line, user, w))
            if not _is_root(user):
                bad.append(f"Dockerfile:{line} runs as "
                           f"{user or 'the image default user'} and writes "
                           f"{'; '.join(w[:4])}")
    return bad, writers


# ---------------------------------------------------------------- the file

def test_every_pdk_write_in_the_dockerfile_runs_as_root():
    bad, _ = audit(DOCKERFILE.read_text(encoding="utf-8"))
    assert not bad, (
        "a RUN writes under /foss/pdks as a non-root user; /foss/pdks is "
        "root-owned, so the build fails there (vibeic-eda#240):\n  "
        + "\n  ".join(bad))


def test_the_check_sees_the_writes_this_file_makes():
    """Non-vacuity: a detector that finds nothing passes every file."""
    _, writers = audit(DOCKERFILE.read_text(encoding="utf-8"))
    seen = "\n".join(e for _, _, w in writers for e in w)
    for expect in (
            # the loop this test exists for: a variable, then mkdir/install
            "mkdir /foss/pdks/ihp-sg13g2/libs.tech/xyce/plugins",
            "install /foss/pdks/ihp-sg13g2/libs.tech/xyce/plugins/Xyce_Plugin_$name.so",
            # a relative -o resolved against `cd .../verilog-a`
            "openvaf-r /foss/pdks/ihp-sg13g2/libs.tech/ngspice/osdi/$M.osdi",
            # an --option output
            "python3 /foss/pdks/LOCAL_MODIFICATIONS.json"):
        assert expect in seen, f"{expect!r} not detected; saw:\n{seen}"
    assert len(writers) >= 6, writers


def test_a_run_that_only_reads_the_pdk_is_not_a_writer():
    """Non-unanimity: the baseline digest names /foss/pdks and writes elsewhere."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    readers = [ln for ln, op, body in instructions(text)
               if op == "RUN" and PDKS in body and not pdk_writes(_shell_of(body))]
    assert readers, "every RUN naming /foss/pdks was called a writer"
    assert not pdk_writes(
        'set -e; d=$(mktemp -d); X=/foss/pdks/ihp-sg13g2/libs.tech; '
        'cp -r "$X/verilog-a/psp103/." "$d/"; ls -la "$X/xyce/plugins"; '
        'Xyce -plugin $X/xyce/plugins/Xyce_Plugin_PSP103_VA.so x.cir > "$d/x.log" 2>&1; '
        'readlink -f /foss/pdks/sky130A; '
        'python3 /vibeic/pdk/pdk_local_mods.py baseline --root /foss/pdks/ciel '
        '--out /vibeic/pdk/volume-as-delivered.sha256')


# --------------------------------------------------------- red on the defect

_PR240_SHAPE = r"""
FROM base
USER root
RUN echo tools
USER 1000
RUN set -e; \
    d=$(mktemp -d); cd "$d"; \
    buildxyceplugin -o vgate vgate.va . > build.log 2>&1 || true; \
    cd /; rm -rf "$d"; \
    X=/foss/pdks/ihp-sg13g2/libs.tech; mkdir -p "$X/xyce/plugins"; \
    for m in psp103:PSP103_VA r3_cmc:r3_cmc mosvar:mosvar; do \
      src=${m%%:*}; name=${m#*:}; b=$(mktemp -d); \
      cp -r "$X/verilog-a/$src/." "$b/"; \
      ( cd "$b" && buildxyceplugin "$b/$src.va" "$b" ) > "$b/build.log" 2>&1 || true; \
      install -m 0644 "$b/libXyce_Plugin_$name.so" "$X/xyce/plugins/Xyce_Plugin_$name.so"; rm -rf "$b"; \
    done
USER root
RUN python3 /vibeic/pdk/pdk_local_mods.py verify --record /foss/pdks/LOCAL_MODIFICATIONS.json
USER 1000
"""


def test_the_pr240_layout_is_red():
    bad, _ = audit(_PR240_SHAPE)
    assert len(bad) == 1 and "runs as 1000" in bad[0], bad
    assert "xyce/plugins" in bad[0]


def test_moving_the_plugin_loop_back_into_the_uid_1000_run_is_red():
    """The same defect made on THIS file, not on a snippet."""
    src = DOCKERFILE.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, ln in enumerate(src)
                 if ln.lstrip().startswith('X=/foss/pdks/ihp-sg13g2/libs.tech; mkdir -p "$X/xyce/plugins"'))
    end = next(i for i in range(start, len(src))
               if "cmos5l plugin links resolve" in src[i])
    block = src[start:end + 1]
    del src[start:end + 1]
    probe = next(i for i, ln in enumerate(src) if "buildxyceplugin -o vgate" in ln)
    probe_end = next(i for i in range(probe, len(src))
                     if src[i].strip() == 'cd /; rm -rf "$d"')
    users = [ln.strip() for ln in src[:probe] if ln.startswith("USER ")]
    assert users[-1] == "USER 1000", f"the builder probe now runs as {users[-1]}"
    src[probe_end] = src[probe_end].rstrip("\n") + "; \\\n"
    block[-1] = block[-1].rstrip().rstrip("\\").rstrip().rstrip(";") + "\n"
    mutated = "".join(src[:probe_end + 1] + block + src[probe_end + 1:])
    bad, _ = audit(mutated)
    assert bad and all("runs as 1000" in b for b in bad), bad
    assert any("xyce/plugins" in b for b in bad), bad


def test_a_comment_cannot_change_the_verdict():
    quiet = ("FROM base\nUSER 1000\nRUN set -e; \\\n"
             "    # mkdir -p /foss/pdks/ihp-sg13g2/libs.tech/xyce/plugins\n"
             "    echo ok\n")
    assert audit(quiet)[0] == []
    loud = ("FROM base\nUSER 1000\n# this RUN is root, trust me\nRUN set -e; \\\n"
            "    # runs as root\n"
            "    touch /foss/pdks/versions.txt\n")
    assert len(audit(loud)[0]) == 1


def test_from_resets_the_user_and_the_image_default_is_not_root():
    text = ("FROM a\nUSER root\nRUN mkdir -p /foss/pdks/x\n"
            "FROM b\nRUN mkdir -p /foss/pdks/y\n")
    bad, writers = audit(text)
    assert len(writers) == 2
    assert len(bad) == 1 and "/foss/pdks/y" in bad[0] and "default" in bad[0]


def test_an_unparseable_pdk_run_fails_loudly():
    with pytest.raises(Unparseable):
        audit("FROM a\nUSER 1000\nRUN echo 'unclosed > /foss/pdks/x\n")
