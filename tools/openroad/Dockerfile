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
# OPENROAD_REPO / OPENROAD_REF are declared just above the clone, not here. An
# ARG changes the cache key of EVERY later RUN in the stage, whether or not the
# RUN reads it -- so declared here, each daily pin move re-ran the tcl and llvm
# prefetches below. See the note beside the clone.
# `build --stamp` in the USER rc, because `etc/Build.sh` used to build its bazel
# argument list from a closed set (`--jobs`, `--config=opt` from -lto,
# `--//:platform=`) with no pass-through, so `--config=release` could not be
# handed to it. Upstream f8c0deee560 (2026-09-10) made Build.sh pass
# `--config=release` itself, which sets `--stamp`; the user-rc line written in
# the clone RUN is redundant from that commit on and still stamps a pin that
# predates it. Bazel reads
# ~/.bazelrc automatically and this stage runs as root; the workspace
# `.bazelrc` already supplies
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
# THE ONE ARCHIVE THIS HOST CANNOT FETCH. `MODULE.bazel` carries
# `bazel_dep(name = "tcl_lang", version = "9.0.2.bcr.2")`, and the BCR record
# for it names exactly one source:
#
#     https://downloads.sourceforge.net/project/tcl/Tcl/9.0.2/tcl9.0.2-src.tar.gz
#
# MEASURED 2026-09-15: SourceForge returns HTTP 403 to this host for
# EVERYTHING -- that file, an unrelated libpng tarball, the tcl project page
# and sourceforge.net itself -- over IPv4 and IPv6, from the host and from
# both docker networks, with curl, wget and python, with and without a
# browser User-Agent. The body is a challenge page, so it is an egress-IP
# block, not a dead link and not a flaky mirror. Every `*.dl.sourceforge.net`
# mirror 301s straight back to the blocked redirector.
#
# The build did not fail for a reason in the code: bazel could not fetch, so
# `//:openroad` failed analysis, the release refused to tag a version, and the
# public page stopped publishing.
#
# `--distdir` is bazel's own answer: a directory of pre-fetched archives it
# consults by basename before reaching the network, still checking the
# integrity the registry recorded. So the archive is fetched here, from a
# source that answers, and verified against the BCR hash before bazel runs --
# a wrong or truncated file fails THIS layer with a clear message instead of
# failing the build 25 minutes later inside bazel.
#
# Software Heritage is first because it is content-addressed and permanent:
# the URL names the sha256, so it cannot silently serve different bytes.
# SourceForge stays as the second source, unchanged, so the day the block
# lifts this keeps working with no edit.
ARG TCL_SRC_SHA256=e074c6a8d9ba2cddf914ba97b6677a552d7a52a3ca102924389a05ccb249b520
RUN mkdir -p /distdir \
 && for u in \
      "https://archive.softwareheritage.org/api/1/content/sha256:${TCL_SRC_SHA256}/raw/" \
      "https://downloads.sourceforge.net/project/tcl/Tcl/9.0.2/tcl9.0.2-src.tar.gz" ; do \
      curl -fsSL --retry 2 --retry-delay 5 -m 600 \
           -o /distdir/tcl9.0.2-src.tar.gz "$u" && break ; \
    done \
 && echo "${TCL_SRC_SHA256}  /distdir/tcl9.0.2-src.tar.gz" | sha256sum -c -

# The same failure, a larger archive, and a different cause: measured 2026-09-25.
#
# Upstream moved the build onto hermetic-llvm (BCR module "llvm" 0.8.18, a dev
# dependency of the root module), so a cold build now fetches
# llvm-project-22.1.8.src.tar.zst -- 171,128,113 bytes -- from GitHub release
# assets. The 05:30 round died on it 4065 s into the openroad build step:
#
#     WARNING: Download from .../llvm-project-22.1.8.src.tar.zst failed:
#         class java.io.IOException GET returned 618 jwt:expired
#     invalid registered toolchain '@llvm//toolchain:all'
#
# Not a flake. This site reaches GitHub's release CDN at 41-191 KB/s, measured
# from four hosts (8HD-d 41-136, 8HD-7 58, 8HD-3 119, 8HD-4 191), so the archive
# takes 15-70 minutes. A release download is a redirect to a SIGNED URL that
# expires within minutes; any retry or resume that reuses it gets `618
# jwt:expired`. Bazel has one URL and no mirror, so one expiry kills the round.
#
# The fix is to never hold a signed URL long: each attempt below requests the
# github.com URL afresh -- a new redirect, a new signature -- and resumes from
# the bytes already on disk (`-C -`), bounded to 240 s so it finishes well
# inside a signature's life. Measured on this host: 5 attempts, 1178 s, and the
# result hashes to exactly what hermetic-llvm 0.8.18's llvm_versions.json
# records, below. Software Heritage does not carry this archive (404), so the
# tcl block's content-addressed first source has no equivalent here.
#
# `llvm_project_archive` calls `rctx.download(..., sha256, ...)`, so bazel's
# --distdir finds this by basename and still verifies it. If upstream moves to
# another LLVM, this file simply stops matching and bazel fetches the new one
# itself -- the prefetch is an accelerator and cannot pin the wrong compiler.
# This layer is cached across OPENROAD_REF moves because that ARG is declared
# only after it, right above the clone. (vibeic-eda#232 said this layer was cached
# because it sat before the clone; that was false while the ARG was declared at
# the top of the stage -- see the note beside the clone.)
ARG LLVM_SRC_SHA256=0a120cd02aa4319887b938ed98f34907563e9b4bcdd4a2aed23a1a05dd5a8157
RUN f=/distdir/llvm-project-22.1.8.src.tar.zst \
 && u=https://github.com/hermeticbuild/llvm-redist/releases/download/llvmorg-22.1.8-r1/llvm-project-22.1.8.src.tar.zst \
 && for i in $(seq 1 60); do \
      echo "${LLVM_SRC_SHA256}  $f" | sha256sum -c - >/dev/null 2>&1 && break ; \
      curl -fsSL -C - --max-time 240 -o "$f" "$u" || true ; \
    done \
 && echo "${LLVM_SRC_SHA256}  $f" | sha256sum -c -

# ---- why OPENROAD_REPO / OPENROAD_REF are declared HERE -----------------------
#
# An ARG changes the cache key of every later RUN in its stage, whether or not
# the RUN reads it. Declared at the top of the stage, as they were until this
# note, each daily pin move re-ran the tcl and llvm prefetches above although
# neither reads the pin. Measured in the 2026-09-25 release: the llvm prefetch
# step took 2364.1 s re-fetching a 171 MB archive that had not changed, ahead
# of a 3265.6 s bazel step. With the ARGs declared here, a build at
# f3c1be109b followed by one at ae138e2ea9 on the same builder reported the
# llvm prefetch as CACHED; those layers are now keyed only on what they fetch.
#
# Pinned to the F-A2 commit (tapcell -bound_to_placement) shipped in vibeic-eda:0.2.7,
# a fast-forward over the F-A1 repair_antennas -reroute commit (0.2.6).
# HEAD of branch vibeic/post-route-detailed-routing-repair as of 2026-07-09.
ARG OPENROAD_REPO=https://github.com/vibeic/OpenROAD.git
ARG OPENROAD_REF=ae138e2ea9a97693d68c16819440c0e90b1afdaa  # pinned; branch master (0.2.22 consolidation: route DR6 + place FP2 + pdn PD2/PD4 + timing/fill/cts/dpl-leak; src/sta=1e21c3f superset fetched from vibeic/OpenSTA fork; commit-msg NDA-purged 2026-07-19) -- moved off vibeic/grt-310-collision 2026-08-03: master had absorbed it (git cherry empty) and carried the GRT message-id fix that branch lacks, without which the tool does not build. See vibeic-eda#66. -- advanced 2026-08-05 to f396ce8ee, our own `drt: verify the routed design`: the pin sat one commit behind the fork tip, so the DRC verifyRoute gate was built but never shipped (fork_gap_report Q2 release_lag=1). -- advanced 2026-09-22 to `drt: detailed_route -nets`, a route that re-lays ONLY the named nets and holds every other net's dbWire byte-identical, and `repair_antennas -reroute` now using it. Without it a one-diode antenna repair on a 655-net design lost 93 other nets' wire entirely, shrank 55, and ended in DRT-0206, so the flow could only roll the repair back and ship the violations. -- advanced 2026-09-22 to `rcx: extraction must not change the routing it measures`: Ext::extract() opens with odb::orderWires(), which rewrites every non-supply net's dbWire through tmg_conn::analyzeNet(); that rewrite is not geometry-preserving and turned a DRC-clean detailed route into 599 whole-design violations (547 Short, 45 Metal Spacing, 7 NS Metal) with nothing reporting it, so the flow shipped the damaged route and a downstream gate refused 450 log lines later holding a bare count. The raw wire stream is now saved before the ordering and replayed after it; the SPEF is byte-identical either side. Also: a scoped route now measures its byte-identical claim on odb (DRT-0709) and reports a whole-design entry-vs-exit delta instead of comparing a design-wide count with a tile-local one (DRT-0711/0712), and repair_antennas -reroute can pass -output_drc to the scoped route it runs.
RUN git clone "${OPENROAD_REPO}" /src \
 && cd /src && git checkout ${OPENROAD_REF} \
 && git submodule update --init --recursive --depth 1 \
 && printf 'build --stamp\nbuild --distdir=/distdir\n' > /root/.bazelrc \
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
