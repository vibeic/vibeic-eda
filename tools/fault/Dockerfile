# eda-tool-fault — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-fault:<ref>`. The tag IS the pin.
#
# WHY THIS TOOL IS NEW HERE (2026-07-31). `fault` was the ONE EDA tool the flow
# invokes that this build never produced: it arrived from the iic-osic-tools base
# image at /usr/local/bin/fault and the Dockerfile only symlinked it into
# /foss/tools/bin. Because it was never cloned, `check_fork_only.py` had nothing
# to check, it never appeared in FORKS.json, and the daily upstream merge never
# saw it — the tool was invisible to every mechanism that keeps a tool current.
# Measured consequence: the inherited binary is 0.9.4, built from a 2021 branch
# (`efabless/Fault`, itself a stale fork), while the live upstream is AUCOHL/Fault.
#
# It also carries our own fix, which is why shipping the base's copy is not an
# option: `fault chain -o out.v --skip-synth` used to exit EX_OK and never create
# `out.v` — the netlist, the FAULT METADATA header and the chain verification all
# landed in the intermediate file instead. An automated flow saw success and no
# artefact.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29);
# `tools/check_fork_only.py` fails the build on anything else.

FROM swift:5.9 AS build
ARG FAULT_REPO=https://github.com/vibeic/Fault.git
ARG FAULT_REF=fix/skip-synth-honours-output  # branch: carries the --skip-synth output fix
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${FAULT_REPO}" /fault \
 && git config --global --add safe.directory /fault \
 && cd /fault && git checkout ${FAULT_REF} \
 && swift build -c release
# The binary plus the Swift runtime it links. `ldd` on the built artefact names
# libswiftCore / libFoundation / libdispatch / libBlocksRuntime / libicu*swift —
# none of which exist in the EDA base, so the binary alone would not start.
RUN mkdir -p /out/bin /out/lib \
 && cp /fault/.build/release/fault /out/bin/fault \
 && for so in $(ldd /fault/.build/release/fault | grep -oE '/usr/lib/swift/linux/[^ ]+\.so[0-9.]*'); do cp -L "$so" /out/lib/; done \
 && cp -L /usr/lib/swift/linux/x86_64/*.so /out/lib/ 2>/dev/null || true \
 && ls /out/lib | head -30

FROM scratch AS artefact
COPY --from=build /out/bin/fault /foss/tools/fault/bin/fault
COPY --from=build /out/lib/ /foss/tools/fault/lib/
