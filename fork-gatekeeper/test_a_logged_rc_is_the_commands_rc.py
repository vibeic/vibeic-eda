"""A logged exit code must be the command's, not `date`'s.

2026-09-30: `check_release_failed` could not raise its alert (gh refused the
body), printed [COULD NOT RAISE] and returned 1.  The round logged

    [2026-09-30T...] check_release_failed exit 0 (0=nothing to raise, ...)

because the line was `echo "[$(date -Is)] check_release_failed exit $? ..."`:
bash expands the words left to right, `$(date -Is)` RUNS first, and `$?` is
then `date`'s status.  `bash -c 'false; echo "[$(date -Is)] rc=$?"'` prints
rc=0.  build_image.sh and build_image_magic.sh logged their smoke the same way.

The rule: within one simple command, no `$?` after a command substitution --
`$(...)` or backticks; `$((...))` is arithmetic and runs nothing.  A `$?` in a
LATER command (after `;`, `&&`, `||`, `|` or a newline) is a different read and
is fine: `x=$(cmd); rc=$?` is how a substitution's status is meant to be read.

The scanner is a small lexer, not a regex, because the real files carry the
cases a regex gets wrong: comments that quote the bad pattern, `;`-separated
captures after a `$(dirname ...)`, arithmetic, and python heredocs inside
`$( ... )`.  It is calibrated against bash itself before it is trusted.

This guard is local to a simple command: it does not trace variable data flow
such as TS="$(date)"; echo "$TS $?". Capture status before any separate command.
"""
from __future__ import annotations

import re
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCAN_DIRS = (HERE, ROOT / "tools")

_HEREDOC = re.compile(
    r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_SUBST = ("cs", "bt")


def scan(src: str):
    """Return (stale_reads, unscanned).

    stale_reads: line numbers where `$?` is read after a command substitution
    ran earlier in the same top-level simple command.
    unscanned: line numbers of `$?` inside an UNQUOTED heredoc body, which
    bash expands but this lexer skips -- reported so the blind spot is loud.
    """
    stale, unscanned = [], []
    stack: list = []   # sq dq cs($( ) bt(`) ar($(( ) p(paren inside cs/ar)
    seen = False
    heredocs: list = []
    line, i, n = 1, 0, len(src)
    while i < n:
        c = src[i]
        top = stack[-1] if stack else None
        if top == "sq":
            if c == "'":
                stack.pop()
            elif c == "\n":
                line += 1
            i += 1
            continue
        if top == "aq":  # ANSI-C quotes allow escaped single quotes.
            if c == "\\":
                if src[i + 1:i + 2] == "\n":
                    line += 1
                i += 2
            else:
                if c == "'":
                    stack.pop()
                elif c == "\n":
                    line += 1
                i += 1
            continue
        if c == "\n":
            line += 1
            i += 1
            if heredocs and top not in ("dq", "bt"):
                for delim, strip, quoted in heredocs:
                    opened, closed = line - 1, False    # the `<<` line
                    while i < n:
                        end = src.find("\n", i)
                        end = n if end < 0 else end
                        body = src[i:end]
                        i = min(end + 1, n)
                        if (body.lstrip("\t") if strip else body) == delim:
                            line += 1
                            closed = True
                            break
                        if not quoted and ("$?" in body or "${?}" in body):
                            unscanned.append(line)
                        line += 1
                    if not closed:
                        # A misread `<<` would otherwise swallow the rest of
                        # the file and report it clean.
                        unscanned.append(opened)
                heredocs = []
            if not stack:
                seen = False
            continue
        if c == "\\":
            if src[i + 1:i + 2] == "\n":
                line += 1
            i += 2
            continue
        if (c == "#" and top in (None, "cs", "p")
                and (i == 0 or src[i - 1] in " \t\n;&|()")):
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "'" and top != "dq":
            stack.append("sq")
            i += 1
            continue
        if src.startswith("$'", i) and top != "dq":
            stack.append("aq")
            i += 2
            continue
        if c == '"':
            if top == "dq":
                stack.pop()
            else:
                stack.append("dq")
            i += 1
            continue
        if src.startswith("$((", i):
            stack.append("ar")
            i += 3
            continue
        if src.startswith("$(", i):
            if not any(s in _SUBST for s in stack):
                seen = True
            stack.append("cs")
            i += 2
            continue
        if c == "`":
            if top == "bt":
                stack.pop()
            else:
                if not any(s in _SUBST for s in stack):
                    seen = True
                stack.append("bt")
            i += 1
            continue
        if c == "(" and top in ("cs", "ar", "p"):
            stack.append("p")
            i += 1
            continue
        if c == ")" and top in ("cs", "ar", "p"):
            if top == "ar":
                if src.startswith("))", i):
                    stack.pop()
                    i += 2
                    continue
                i += 1
                continue
            stack.pop()
            i += 1
            continue
        if src.startswith("<<<", i):
            # A here-string, not a heredoc: stepping one `<` at a time would
            # read its `<< "${VAR}"` tail as a heredoc delimiter.
            i += 3
            continue
        if src.startswith("<<", i) and top in (None, "cs", "p"):
            m = _HEREDOC.match(src, i)
            if m:
                quoted = m.group(4) is None or src[m.start(4) - 1] == "\\"
                delim = next(g for g in m.group(2, 3, 4) if g is not None)
                heredocs.append((delim, m.group(1) == "-", quoted))
                i = m.end()
                continue
        if src.startswith("$?", i) or src.startswith("${?}", i):
            if seen and not any(s in _SUBST for s in stack):
                stale.append(line)
            i += 4 if src.startswith("${?}", i) else 2
            continue
        if not stack and c in ";&|" and not (
                c == "&" and i > 0 and src[i - 1] in "<>"):
            seen = False
        i += 1
    if stack:
        unscanned.append(line)
    return stale, unscanned


# Each snippet runs right after `false` and prints `rc=<the $? it read>`.
# Stale means it printed 0: something ran between `false` and the read.
_CALIBRATION = [
    'echo "[$(date -Is)] rc=$?"',
    "echo $(true) rc=$?",
    'echo "`true` rc=$?"',
    'echo "[$(printf %s "$(date)")] rc=$?"',
    'echo "${UNSET_VAR:-$(date)} rc=$?"',
    'echo "[$(date -Is)] \\\nrc=$?"',
    'echo "$(date)" \\\n  "rc=$?"',
    'echo "rc=$? at $(date +%s)"',
    'echo "$((1 + 1)) rc=$?"',
    'x=$(false); echo "rc=$?"',
    'echo "\\$(date) rc=$?"',
    "echo '$(date)' \"rc=$?\"",
    '# echo "[$(date)] rc=$?"\necho "rc=$?"',
    'echo "rc=$?"; echo "$(date)" >/dev/null',
    # A here-string before the read must not open a heredoc that swallows it.
    'read -r _ <<< "${HOME}"\nfalse\necho "[$(date -Is)] rc=$?"',
]


@pytest.mark.parametrize("snippet", _CALIBRATION)
def test_the_scanner_agrees_with_bash(snippet):
    """The instrument first: its verdict must be bash's own."""
    out = subprocess.run(["bash", "-c", "false\n" + snippet],
                         capture_output=True, text=True, timeout=30).stdout
    read = re.search(r"rc=(\d+)", out)
    assert read, out
    bash_says_stale = read.group(1) == "0"
    stale, _ = scan(snippet)
    assert bool(stale) == bash_says_stale, (snippet, out, stale)


def test_a_quoted_heredoc_body_is_data():
    src = ("python3 - <<'PY'\nprint(\"$(date) $?\")\nPY\n"
           'X="$(python3 - <<\'PY\'\nprint(1)  # it\'s a comment\nPY\n)"\n'
           'rc=$?\n')
    assert scan(src) == ([], [])


def test_what_the_scanner_cannot_read_is_reported_not_passed():
    """An unquoted heredoc body is expanded by bash but not lexed here, and an
    unterminated one would swallow the rest of the file; both are named."""
    assert scan("cat <<EOF\nrc=$?\nEOF\n") == ([], [2])
    assert scan("cat <<EOF\necho hi\n") == ([], [1])


def _scripts():
    found = set(ROOT.glob("*.sh"))
    for base in (ROOT, ROOT / "fork-gatekeeper", ROOT / "tools"):
        paths = base.iterdir() if base == ROOT else base.rglob("*")
        for path in paths:
            if not path.is_file():
                continue
            if path.suffix == ".sh":
                found.add(path)
            elif not path.suffix:
                with path.open("rb") as f:
                    first = f.readline(256).decode("utf-8", errors="replace")
                if re.match(r"^#!.*(?:/|\s)(?:bash|sh|dash|ksh|zsh)(?:\s|$)", first):
                    found.add(path)
    return sorted(found)


def test_no_script_logs_a_substitutions_status_as_a_commands():
    scripts = _scripts()
    assert any(p.name == "run_0530.sh" for p in scripts), "scan found no round"
    stale, blind = [], []
    for path in scripts:
        s, u = scan(path.read_text(encoding="utf-8", errors="replace"))
        rel = path.relative_to(ROOT)
        stale += [f"{rel}:{ln}" for ln in s]
        blind += [f"{rel}:{ln}" for ln in u]
    assert not stale, (
        "a `$?` read after `$(...)`/backticks in the same command reads the "
        "substitution's status (e.g. `date`'s 0); capture rc=$? on the line "
        f"after the command and log the variable: {stale}")
    assert not blind, f"`$?` in an unquoted heredoc body is unscanned: {blind}"


_ROUND_STATUS_INPUTS = (
    "SIX", "TICK", "DISC", "PAGE", "FRESH", "CODE", "TESTS", "INPUT_CLEAR",
    "AI_AUTH", "GAP_FATAL", "FORK_GH", "LL", "TERMINAL",
)


@pytest.mark.parametrize("gate,value,expected", [
    (None, 0, 0),
    *[(gate, value, 1) for gate in _ROUND_STATUS_INPUTS for value in (1, 2)],
    ("SILENCE", 1, 0),  # Already escalated: existing advisory contract.
    ("SILENCE", 2, 1),  # Could not check: still blocking.
])
def test_round_terminal_log_matches_its_actual_exit(tmp_path, gate, value, expected):
    """Execute the production terminal block; never infer cron rc from a tick.

    No release commands run here. Every blocking input, including SIX despite
    successful publication, retains its existing nonzero-exit behavior.
    """
    source = (HERE / "run_0530.sh").read_text()
    marker = "# 0 only when BOTH are clean:"
    assert source.count(marker) == 1
    terminal = source[source.index(marker):]
    log = tmp_path / "round.log"
    log.touch()
    env = dict(os.environ, LOG=str(log))
    env.update({key: "0" for key in (*_ROUND_STATUS_INPUTS, "SILENCE")})
    if gate:
        env[gate] = str(value)
    result = subprocess.run(["bash", "-uc", terminal], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, (gate, result.stdout, result.stderr)
    assert re.search(rf"run_0530 exit {expected}(?:\s|$)", log.read_text()), (
        gate, result.returncode, log.read_text())
