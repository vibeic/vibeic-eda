# Releasing vibeic-eda

The image version bumps **whenever any forked tool is upgraded** (a new pinned SHA
in the [`Dockerfile`](./Dockerfile)). Every place that names the image must move in
lockstep — that propagation is automated so it is fool-proof and cannot drift.

## Source of truth

[`VERSION`](./VERSION) — one line, `X.Y.Z`. Everything else is derived from it.

The image tag `vibeic-eda:X.Y.Z` appears in the install docs (README) as a **live
pointer**; the same short form in prose ("fix shipped in `vibeic-eda:0.2.5`") and in
`FIX_STATUS.md` is **history** and is left alone. The two are told apart
automatically — see [`sync_image_version.py`](./sync_image_version.py).

## Bump + propagate (one command)

```bash
# a fork SHA changed in the Dockerfile → cut the next version:
./sync_image_version.py --bump patch     # or: --set 0.3.0   (--dry-run to preview)
```

This rewrites the VERSION file **and** every live pointer in the docs, then
re-checks that nothing is left behind. `--check` (the default, no args) verifies
sync and is what CI runs on every push/PR (`.github/workflows/version-sync.yml`) —
a forgotten reference is a hard failure, including any new `ghcr.io/...:X.Y.Z`
pull pointer anywhere in the repo (the drift net).

## Build + publish

```bash
docker build -t vibeic/vibeic-eda:$(./sync_image_version.py --print) .   # from-source, SHA-pinned
./release.sh $(./sync_image_version.py --print)                          # tag + push GHCR (+ Docker Hub)
```

Or push a git tag `vX.Y.Z` to run the from-source build on the self-hosted
`vibeic-builder` runner (`.github/workflows/release.yml`). That workflow first runs
the **version-sync guard** — it refuses to build if `VERSION` ≠ the release tag — and
its smoke test drives a **bare `docker exec` (no login shell)** so a regression of
the on-PATH tool resolution fails the release.

## Checklist

1. Update the fork SHA(s) in `Dockerfile`; add a `FIX_STATUS.md` entry.
2. `./sync_image_version.py --bump patch` (or `--set X.Y.Z`).
3. `git commit -am "X.Y.Z — <what changed>"`.
4. Build + `./release.sh X.Y.Z` (or push tag `vX.Y.Z`).
5. `git tag vX.Y.Z && git push --tags` (if you didn't tag-trigger).
