# Releasing vibeic-eda

The image version bumps **whenever any forked tool is upgraded**. Every place that
names the image must move in lockstep — that propagation is automated so it is
fool-proof and cannot drift.

Since 2026-07-29 each tool is built as its own artefact and the image composes
them; the `Dockerfile` compiles nothing. **A tool upgrade is therefore a
three-file change, not a one-line one** — see [`tools/README.md`](./tools/README.md)
and the checklist below. `tools/check_pins_agree.py` fails the build if you move
one and not the others, because a pin that disagrees with itself does not break
the build, it ships a release containing something other than what it says.

## Source of truth

[`VERSION`](./VERSION) — one line, `X.Y.Z`. Everything else is derived from it.

The image tag `vibeic-eda:X.Y.Z` appears in the install docs (README) as a **live
pointer**; the same short form in prose ("fix shipped in `vibeic-eda:0.2.5`") and in
`FIX_STATUS.md` is **history** and is left alone. The two are told apart
automatically — see [`sync_image_version.py`](./sync_image_version.py).

## Bump + propagate (one command)

```bash
# a fork SHA moved → cut the next version:
./sync_image_version.py --bump patch     # or: --set 0.3.0   (--dry-run to preview)
```

This rewrites the VERSION file **and** every live pointer in the docs, then
re-checks that nothing is left behind. `--check` (the default, no args) verifies
sync and is what CI runs on every push/PR (`.github/workflows/version-sync.yml`) —
a forgotten reference is a hard failure, including any new `ghcr.io/...:X.Y.Z`
pull pointer anywhere in the repo (the drift net).

## Build + publish

```bash
python3 fork-gatekeeper/daily_release.py --eda-root .
```

**That one command is the whole transaction, and it is the only publisher.**
`./release.sh` is RETIRED — it refuses with exit 2 and has since #137
(2026-08-18), because publishing a version tag and `latest` without atomically
publishing `RELEASED.json` created a second release authority. Do not reach for
it; it will stop you, which is the point.

`daily_release.py` moves every pin to its fork's tip, builds only the artefacts
that do not exist yet, composes the image (tagged with the version it selected,
so `:latest` is never moved onto an unsmoked candidate), runs
`tools/check_image_depth.py --compact` and the runtime smoke, refuses unless the
registry says the version tag is absent, pushes it, freezes the remote manifest
identity, writes `VERSION` and `RELEASED.json`, commits and pushes that record —
and only then moves `latest`. It chooses the version itself, above both the
`VERSION` file and every tag that already exists, so do not bump the file first
on this path.

Useful subcommands:

```bash
--dry-run                 # what would move, what would be built, whether a release is needed
--record-release X.Y.Z    # write RELEASED.json for an image published OUTSIDE this program
--repair-latest X.Y.Z     # point `latest` at an already-published version
--smoke-only IMAGE        # run the tool smoke against an existing image and stop
```

The two bake commands it drives are still the right ones to run by hand when you
are iterating rather than releasing:

```bash
docker buildx bake tools --push    # build and push tool artefacts
docker buildx bake eda             # compose the image from those artefacts
```

but note what the first one actually does. **The skip is not in bake.** Bake
builds every target in the group it is given; the cheapness of a one-pin release
comes from the CALLER asking the registry first. `.github/workflows/release.yml`
does it with `bake --print tools` and a per-target existence check, and
`daily_release.py` does it with `missing_artefacts()`. Run the bare `tools`
group with `--push` when every pin is already published and you rebuild all 19
and overwrite tags that earlier releases pin — a rebuild that is not
bit-identical silently changes what those releases resolve to.

`docker buildx bake eda` on its own is likewise not how a release is composed:
it tags `:${TAG}` (default `dev`) and `:latest`, and stamps the image
`version=dev`. The release path always passes `TAG=` and `--set eda.tags=`.

A plain `docker build .` still works, but **only once the artefacts it pins are
published** — it copies them in, it does not build them. If a pin names an image
that does not exist the build fails with `not found`, which is the correct and
intended failure: better a loud stop than a release quietly built from something
other than the pin.

To iterate on a tool without pushing first, `docker buildx bake eda-local` builds
the tools locally and redirects the COPYs at them. It **ignores the pins**, so a
green `eda-local` says the composition works and nothing about what a release
would contain.

Or push a git tag `vX.Y.Z` to run the build on the self-hosted `vibeic-builder`
runner (`.github/workflows/release.yml`). It runs as two jobs: `tools` publishes
any artefact whose pin moved (skipping the rest — that skip is what makes a
one-pin release cheap), then `build-and-push` composes. Before either, the
**version-sync guard** refuses to build if `VERSION` ≠ the release tag.

Two runtime checks run before the push, and they answer different questions. The smoke
test drives a **bare `docker exec` (no login shell)**, so a regression of on-PATH
tool resolution fails the release — but it can only prove the tools RUN. Drop a
`COPY` and the base image's own copy answers every one of those commands.
`tools/check_image_provenance.py` reads the provenance each artefact carries and
asserts the image contains the commits the pins name.

The compose is loaded locally before either registry tag moves. The release path
runs the millisecond-scale `tools/check_dockerfile_layer_budget.py` before the
build: the pinned base is measured at 57 layers and the final stage may spend at
most 53 more, keeping the source prediction at or below 110. A base digest move
must update its measured count in `tools/image_layer_budget.json`; another
`RUN`, `COPY`, or `ADD` that spends the remaining reserve fails the fast gate.

The same program grades the **derived chain**, which is what the fleet actually
pulls. `Dockerfile.derived` builds FROM the previous release's digest, so its
depth is that release's measured depth plus this generation's own filesystem
instructions: 116 + 2 = 118 for 0.3.48, against the 125-layer pull limit. Here the
ceiling is the limit itself and the reserve is ONE GENERATION — when the headroom
no longer fits another generation the gate says SQUASH by name, because a smaller
generation only postpones the same wall. `tools/build_derived.sh` runs this
prediction before the build and `tools/check_image_depth.py` on the bytes after
it; before vibe-ic#2079 that path ran neither, and the root prediction reported
`109/110` no matter how deep the chain got.

After composition, the release path
runs `tools/check_image_depth.py --compact`: a candidate above 125 filesystem
layers is flattened automatically, its runtime configuration is restored and
compared field-for-field for the supported Docker runtime contract, and its
depth is measured again. The resulting candidate must then pass the runtime
smoke; only afterward does `daily_release.py` push the version tag — and
`latest` moves later still, after the record has reached `main`. This order is
load-bearing: an over-depth image can build, run locally, and push successfully
while every fresh `docker pull` fails.

`tools/check_image_provenance.py` is NOT part of that publish path. It is wired
into `.github/workflows/release.yml`,
which has never run, and into `fork-gatekeeper/run_tick.sh`, which runs it the
morning AFTER a release. Run it by hand against the candidate if you want the
answer before you push — it is the only check that reads what the image actually
CONTAINS rather than what the source files say.

## Publishing by hand

Sometimes the transaction cannot run whole — the actor is not allowed to push
`main`, or the round is a composition-only release someone wants to gate
themselves. The hand path is the SAME steps in the SAME order, and it records
itself through the same writer, so it does not become a second release
authority. This is the sequence, and it was last walked for `0.3.69`:

```bash
./sync_image_version.py --bump patch                  # VERSION + every live pointer
python3 tools/check_pins_agree.py                     # rc 0
python3 tools/check_fork_only.py                      # rc 0
tools/prepush-gates.sh --full                         # all 8 gates, incl. "pinned images exist"

TAG=X.Y.Z docker buildx bake --builder vibeic-builder -f docker-bake.hcl \
    --load --progress plain --set eda.tags=ghcr.io/vibeic/vibeic-eda:X.Y.Z eda

python3 tools/check_image_depth.py --compact ghcr.io/vibeic/vibeic-eda:X.Y.Z
python3 fork-gatekeeper/daily_release.py --smoke-only ghcr.io/vibeic/vibeic-eda:X.Y.Z
python3 tools/check_image_provenance.py ghcr.io/vibeic/vibeic-eda:X.Y.Z
docker manifest inspect ghcr.io/vibeic/vibeic-eda:X.Y.Z   # MUST be absent before you push
docker push ghcr.io/vibeic/vibeic-eda:X.Y.Z

python3 fork-gatekeeper/daily_release.py --record-release X.Y.Z   # the ONE writer
git commit -am "release: record X.Y.Z as published"
```

`TAG=` and `--set eda.tags=` are both load bearing: the first is what the
`org.opencontainers.image.version` label resolves from, the second is what keeps
the compose off `:latest`.

**`latest` is not part of this sequence and cannot be.**
`authorize_latest_repair()` re-reads `origin/<branch>`'s `RELEASED.json` and
`VERSION` bytes, compares them with the checkout, and requires the record commit
to be on that branch — so `latest` cannot move until the record has landed. Land
the record, then:

```bash
python3 fork-gatekeeper/daily_release.py --repair-latest X.Y.Z
```

Nothing invokes that automatically. Until it is run,
`fork-gatekeeper/check_release_recorded.py` reports the release as INCOMPLETE,
and it is right to.

One gap to know about rather than paper over: `--record-release` cannot write
`manifest_identity`, so a hand-published record carries one binding fewer than a
`daily_release` one. Every reader tolerates its absence — `check_release_recorded`
and `repair_latest_publication` both skip that comparison when it is `None` — but
do not hand-write the field to make the record look complete.

## Checklist

1. Move the tool's SHA in **all three places** it is written:
   - `tools/<name>/Dockerfile` — the `ARG <NAME>_REF` that gets compiled
   - `docker-bake.hcl` — the `variable` that gets tagged
   - `Dockerfile` — the `ARG IMG_<NAME>` tag that gets pulled

   Then `python3 tools/check_pins_agree.py` (exit 0) and add a `FIX_STATUS.md`
   entry. For a multi-source artefact (`sat-solvers`, `lvs`) the tag carries
   **both** commits — `<short1>-<short2>` — so bumping either moves it.
2. `python3 tools/check_fork_only.py` — the source must be a `vibeic/` fork
   (owner rule, 2026-07-29). A genuinely un-forkable source goes in
   `tools/PENDING_FORKS.json` with an expiry, never a bare exception.
3. `python3 tools/check_dockerfile_layer_budget.py` — keep the root composition's
   predicted depth at or below the 110-layer working budget, and the derived chain
   that ships inside the 125-layer pull limit with a generation of reserve. A
   derived generation whose base moved must carry that base's freshly measured
   depth in `tools/image_layer_budget.json` in the same change.
4. `python3 fork-gatekeeper/daily_release.py --eda-root .` — it selects the
   version, builds, gates, publishes, writes `VERSION` and `RELEASED.json`,
   commits that record and moves `latest`. There is no separate bump, commit or
   publish step on this path: it does all three. Bump `VERSION` by hand only on
   the hand-publish path above, where `--record-release` refuses unless `VERSION`
   already names the version you published.

A git tag is NOT part of either path. `daily_release.py` creates none, and the
remote carries exactly one — `v0.2.26`, from before this program existed. The
release identity is `RELEASED.json` plus the immutable registry tag, not a git
tag; tag `vX.Y.Z` yourself only if you specifically want to trigger
`.github/workflows/release.yml`.
