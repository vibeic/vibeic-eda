# Per-tool artefacts

Each EDA tool is built **once**, as its own image keyed on the commit that built
it, and the release image copies those in rather than compiling anything.

```
ghcr.io/vibeic/eda-tool-<name>:<commit-sha>
```

## Why

Until 2026-07-29 the whole toolchain was one 605-line multi-stage `Dockerfile`,
and `release.yml` called `docker/build-push-action` with **neither `cache-from`
nor `cache-to`** — measured: `grep -c "cache-from\|cache-to" .github/workflows/release.yml`
returned `0`. So every release recompiled all eight tools from source whether or
not their pins had moved, and changing one character of `NGSPICE_REF` cost the
same 1–2 hours as changing everything.

That is what made "fork one more tool" read as "double the build time". The cost
was never the new tool. It was that nothing was built once.

Upstream IIC-OSIC-TOOLS never had this problem — it uses
`_build/images/<tool>/Dockerfile` plus a `docker-bake.hcl` DAG. We had flattened
that structure into one file, and the flattening is what removed per-tool build
isolation. This restores it.

## Layout

```
tools/<name>/Dockerfile      one tool; FROM scratch final layer, so the
                             artefact carries the tool directory and nothing
                             else — no compiler, no source tree, no apt lists
docker-bake.hcl              the build DAG (repo root)
Dockerfile                   the composing image: COPY --from=<pinned artefact>
```

## The tag is the pin

`eda-tool-yosys:edb458a` says which commit of `vibeic/yosys` produced those
binaries. "Which yosys is in the image" is answered by reading the tag, not by
trusting a comment next to an `ARG`.

This is also what makes the release cheap: CI skips any tool whose tag already
exists in the registry. That skip is only safe because the tag is a commit — the
same tag can never mean two different commits, so *already published* and
*already correct* are the same statement. A mutable tag (`:latest`, a branch
name) would make the skip silently ship a stale binary, which is why
`check_pins_agree.py` requires every pin to be a full SHA.

Artefacts built from two sources carry both: `eda-tool-sat-solvers:8af8e56-c607304`
(kissat + cadical), `eda-tool-lvs:19185c1-0334b7d` (magic + netgen). Keying the
tag on one of them would mean bumping the other left the tag unchanged, so the
release would keep pulling the image built before the bump — a version change
that silently does not ship.

## Building

```bash
docker buildx bake tools        # every tool artefact
docker buildx bake openroad     # just one
docker buildx bake eda          # the release image, from published artefacts
docker buildx bake eda-local    # the release image, from tools built right now
```

`eda-local` **ignores the pins** — that is what it is for (iterating on a tool
without pushing first) and also its limit. A green `eda-local` says the
composition works; it says nothing about what a release would contain.

## The five checks, and what each one cannot see

| check | asserts | blind to |
|---|---|---|
| `check_fork_only.py` | every source and artefact is `vibeic/` | the base image's own ~35 tools, which we do not fork |
| `check_pins_agree.py` | the three places a pin is written say the same thing | whether that commit exists |
| `check_image_provenance.py` | the **built image** contains the pinned commits | tools the base supplies |
| `check_doc_counts.py` | every count in `README.md` still reproduces from the repo | whether the *sentence* around the number means what it says |
| `release.yml` smoke test | the tools run | *whose build* they came from |

`check_image_provenance.py` and the smoke test exist because of the same failure:
delete one `COPY` line and the base image's own copy of that tool survives, so
every command still runs and every smoke test still passes while the image
quietly ships someone else's binary. Provenance is recorded inside each artefact
during its own build — a tag can be moved, a file in a layer cannot.

`check_doc_counts.py` exists because the counts in the root `README.md` were the
one part of this repo with no generator behind it, and every one of them was
wrong: *15 forked repos* (45 fork repos over 21 upstreams), *13 of them ship*
(15 do), *12 pinned as `ARG`s* (the per-tool split moved the pins into three
places), and *ALIGN is not yet shipped in any image* (it had shipped since
0.2.27). The README now states each count next to the command that produces it,
inside `<!-- counts:local -->` / `<!-- counts:github -->` fences, and the checker
runs those commands. The README's own table is the test; a count that cannot be
regenerated cannot be written down.

Its blind spot is worth stating plainly, because a green run reads as more than
it is: it compares a number to a command's output. It cannot tell you the command
measures the right thing. The first draft of the "vibeic repos cloned by the
build" row returned the correct 15 by counting `.../ngspice` and `.../ngspice.git`
as two repositories — a right answer from a broken measurement, which is the
failure this whole directory keeps finding. Review the command in a diff, not
just the colour of the run.

## Where the checks actually run

`.github/workflows/fork-only.yml` runs `check_fork_only.py` and
`check_pins_agree.py` on every push and PR — but measured 2026-07-29, **no
workflow has ever executed on this repo**: `actions/runs` reports `total_count:
0`, for every workflow including the pre-existing `image-version-sync`, while
`actions/permissions` reports Actions enabled.

So until that is resolved, the workflow is registered and does not fire. What
actually enforces the rule is the **05:30 fork-gatekeeper tick**, which runs the
three repo-only checks against the checked-out repo and logs the verdict; a
failure sets the tick's exit code without gating the upstream tracking it also
does.

`check_doc_counts.py` runs in both places, but not identically: the workflow runs
it offline, and the tick adds `--online` because it already holds a `gh` token.
The two GitHub rows in the README (the org's fork count and its distinct-upstream
count) are the numbers most likely to rot and the only ones no checkout can
verify, so the daily tick is the one thing that can notice. Offline they are
reported as **not verified** rather than skipped silently — a network-dependent
number that quietly counts as checked is worse than one openly marked as dated.

`check_image_provenance.py` runs in `release.yml`, on the self-hosted runner, and
has the same caveat.

## Adding a tool

1. Fork it into `vibeic/` (owner rule, 2026-07-29 — the image consumes our forks
   and nothing else).
2. `tools/<name>/Dockerfile`, pinned to a **commit**, never a branch: a branch
   makes the image's contents depend on when it was built.
3. A target in `docker-bake.hcl`.
4. An `IMG_<NAME>` pin plus the `FROM ... AS img-<name>` alias in `Dockerfile`.
   (BuildKit does not expand variables in `COPY --from=`, hence the alias.)
5. An entry in fork-gatekeeper's `FORKS.json` so the 05:30 cron tracks upstream.

6. Nothing, for the counts in the root `README.md` — `check_doc_counts.py` will
   tell you which ones moved and by how much. Update the stated numbers, never
   the commands, unless the command itself is what got the answer wrong.

`check_fork_only.py` fails the build if the source is not ours;
`check_pins_agree.py` fails it if you miss step 3 or 4; `check_doc_counts.py`
fails it if you miss step 6.

## PENDING_FORKS.json

A named, dated exception for a source that is genuinely not forked yet — GitHub
throttles fork creation with a 403 that has nothing to do with rate limits, and
it can persist for hours. Blocking every build on that would push someone toward
the real damage: deleting the check.

So the exception is allowed and made expensive to keep. Every entry carries an
expiry, an expired entry **fails**, and every clean run prints what is
outstanding — an exception that only appears in failure output is one nobody
reads until it is already permanent.
