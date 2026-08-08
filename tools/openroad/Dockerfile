# eda-tool-openroad — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-openroad:<ref>`. The tag IS the pin: "which
# openroad is in the EDA image" is answered by reading the tag, not by trusting a
# comment beside an ARG.
#
# Split out of the 605-line monolith on 2026-07-29. That file had eight builder
# stages and `release.yml` invoked build-push-action with no `cache-from` and no
# `cache-to` (measured: 0 occurrences), so moving one character of one pin
# rebuilt every tool from source. Per-tool artefacts make a rebuild cost exactly
# the tool that changed.
#
# The build body below is UNCHANGED from the stage it came from — this split is
# structural, and a behaviour change smuggled into it would be indistinguishable
# from a migration bug.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29);
# `tools/check_fork_only.py` fails the build on anything else.

FROM openroad/ubuntu24.04-dev:latest AS build
# Pinned to the F-A2 commit (tapcell -bound_to_placement) shipped in vibeic-eda:0.2.7,
# a fast-forward over the F-A1 repair_antennas -reroute commit (0.2.6).
# HEAD of branch vibeic/post-route-detailed-routing-repair as of 2026-07-09.
ARG OPENROAD_REPO=https://github.com/vibeic/OpenROAD.git
ARG OPENROAD_REF=064126009f2669cde448041773c7634f0fe6ce47  # pinned; branch master (0.2.22 consolidation: route DR6 + place FP2 + pdn PD2/PD4 + timing/fill/cts/dpl-leak; src/sta=1e21c3f superset fetched from vibeic/OpenSTA fork; commit-msg NDA-purged 2026-07-19) -- moved off vibeic/grt-310-collision 2026-08-03: master had absorbed it (git cherry empty) and carried the GRT message-id fix that branch lacks, without which the tool does not build. See vibeic-eda#66. -- advanced 2026-08-05 to f396ce8ee, our own `drt: verify the routed design`: the pin sat one commit behind the fork tip, so the DRC verifyRoute gate was built but never shipped (fork_gap_report Q2 release_lag=1).
# `build --stamp` in the USER rc, because `etc/Build.sh` builds its bazel
# argument list from a closed set (`--jobs`, `--config=opt` from -lto,
# `--//:platform=`) with no pass-through, so `--config=release` cannot be handed
# to it. Bazel reads ~/.bazelrc automatically and this stage runs as root; the
# workspace `.bazelrc` already supplies
# `--workspace_status_command=tools/workspace_status.sh`, so `--stamp` is the
# only missing half. Without it the binary reports `bazel-nostamp` — upstream's
# documented placeholder for a dev build — and cannot say which commit it is
# (vibeic-eda#22), which matters for a toolchain whose whole discipline is
# answering that question.
#
# Verified before building, since a full OpenROAD build is an hour: the clone
# below is FULL (no --depth), the fork carries the 26Q3-style tags
# workspace_status.sh matches on, and `git describe --tags --match
# '[0-9][0-9]Q[0-9]' 92b079b47` already yields `26Q3-951-g92b079b47a`. A shallow
# clone or a tagless fork would have stamped `unknown` and looked like a fix.
#
# `-cmake="-DCMAKE_BUILD_TYPE=Release"` is gone from the invocation: it has been
# dead since upstream made Bazel the default, where Build.sh collects
# cmakeOptions and never uses them. It read as "we build Release" and meant
# nothing.
RUN git clone "${OPENROAD_REPO}" /src \
 && cd /src && git checkout ${OPENROAD_REF} \
 && git submodule update --init --recursive --depth 1 \
 && printf 'build --stamp\n' > /root/.bazelrc \
 && ./etc/Build.sh -threads=$(nproc) \
      -prefix=/vibeic-openroad

# The STANDALONE `sta`, which `-prefix` does NOT install (vibeic-eda#8).
# `packaging/BUILD.bazel` declares `srcs = ["//:openroad"]` — one binary — while
# OpenROAD links OpenSTA only as `//src/sta:opensta_lib`. The executable target
# `//src/sta:opensta` exists, is `visibility = public`, and nothing built it.
#
# The consequence was silent and measurable: the base image's own `sta` survived
# at /foss/tools/openroad/bin/sta (8934800 bytes, Jun 22, byte-identical to
# hpretl/iic-osic-tools) and carried NONE of the vibeic timing-ECO superset —
# 0 of 10 commands, against 10 of 10 inside `openroad`. Nothing errored; an
# absent Tcl command in a script that does not call it looks like a working
# install. `sta_engine_parity_check` (vibe-ic v1.8.45+) now measures both.
#
# Copied out of `bazel-bin` rather than the install prefix because the install
# target is what omits it. The `readlink -f` resolves bazel's output symlink to
# a real path first: `bazel-bin` is a symlink into the output base, and a COPY
# --from of a symlink outside the build context copies the LINK, not the file.
#
# `bazelisk` with `bazel` as fallback, resolved the same way `etc/Build.sh` does
# (its lines 391-393) rather than hardcoded: the first attempt wrote `bazel` and
# the build died with `bazel: not found` in 0.127s, because this image has only
# bazelisk. Mirroring the build script's own resolution means a change to which
# launcher ships cannot break this step while leaving Build.sh working.
RUN cd /src \
 && BZL="$(command -v bazelisk || command -v bazel)" \
 && "$BZL" build --stamp //src/sta:opensta \
 && install -D "$(readlink -f bazel-bin)/src/sta/opensta" \
      /vibeic-openroad/bin/sta \
 && /vibeic-openroad/bin/sta -version

# ---------------------------------------------------------------------------
# Stage 2 — vibeic/yosys (tri-state fanin preservation + modern slang SV frontend)
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"openroad","repo":"%s","ref":"%s"}\n' \
      "${OPENROAD_REPO}" "${OPENROAD_REF}" \
      > /vibeic/provenance/openroad.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /opt/or-tools /opt/or-tools
# THE ARTEFACT'S OWN PATH, not upstream's build layout. `etc/Build.sh`
# defaults to BAZEL since the 2026-07-29 daily merge and installs to
# /install/OpenROAD; the CMake tree at /src/build/bin no longer exists, and
# the build failed here with `failed to calculate checksum of ref ...
# /src/build/bin/openroad: not found` AFTER compiling all 11,875 targets.
# Published at its FINAL runtime path, matching every other artefact here
# (yosys -> /foss/tools/yosys, iverilog -> /foss/tools/iverilog). An earlier
# attempt used /vibeic/bin/openroad and the compose step failed with `cannot
# copy to non-directory`: /vibeic/ is the shared PROVENANCE namespace every
# artefact writes a file into, so a payload directory cannot also live there.
COPY --from=build /vibeic-openroad/bin/openroad /foss/tools/openroad/bin/openroad
# Same path the base image uses, so the composing image's COPY overwrites the
# stale upstream binary rather than landing beside it (vibeic-eda#8).
COPY --from=build /vibeic-openroad/bin/sta /foss/tools/openroad/bin/sta
COPY --from=build /vibeic/provenance/openroad.json /vibeic/provenance/openroad.json
