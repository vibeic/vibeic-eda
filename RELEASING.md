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
docker buildx bake tools --push    # artefacts for any pin that moved; skips the rest
docker buildx bake eda             # compose the image from those artefacts
./release.sh $(./sync_image_version.py --print)      # tag + push GHCR (+ Docker Hub)
```

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
smoke and provenance check; only afterward does `release.sh` push the version
and `latest` tags. This order is load-bearing: an over-depth image can build,
run locally, and push successfully while every fresh `docker pull` fails.

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
4. `./sync_image_version.py --bump patch` (or `--set X.Y.Z`).
5. `git commit -am "X.Y.Z — <what changed>"`.
6. Build + `./release.sh X.Y.Z` (or push tag `vX.Y.Z`).
7. `git tag vX.Y.Z && git push --tags` (if you didn't tag-trigger).
