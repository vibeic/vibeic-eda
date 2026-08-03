# eda-tool-sv2v — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-sv2v:<ref>-<recipe>`. The tag IS the pin.
#
# vibeic-eda#60: before this, the image took sv2v from the BASE image and our
# fork could not reach a user. `reused_ip_rtl_consume` invokes sv2v whenever an
# ingested IP is SystemVerilog, so a patch of ours to the converter mattered and
# had nowhere to go.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29);
# `tools/check_fork_only.py` fails the build on anything else.

# ubuntu:24.04, not `haskell:9.4-slim`: that tag is Debian buster, which is EOL —
# `apt-get update` 404s on every deb.debian.org repository. Measured, first probe
# build. Every other tool image here is ubuntu:24.04 anyway.
FROM ubuntu:24.04 AS build
ARG SV2V_REPO=https://github.com/vibeic/sv2v.git
ARG SV2V_REF=6662fa5da71f87797598060f17728b284b99a9fc  # pinned; branch master — identical to zachjs/sv2v@master today (ahead=0, behind=0)
# The distro's `haskell-stack`, not the get.haskellstack.org installer: that
# script runs its own `apt-get install` and dies on a layer that has already
# cleared /var/lib/apt/lists ("Package 'netbase' has no installation candidate"
# — measured, second probe build). One fewer network dependency, and the apt
# cache is cleared once, at the end.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates make curl xz-utils build-essential \
      libgmp-dev zlib1g-dev haskell-stack \
 && rm -rf /var/lib/apt/lists/* \
 && stack --version
# GHC writes its progress with non-ASCII characters (U+00BB in the dependency
# report), and a container with no UTF-8 locale dies mid-build on
# `commitAndReleaseBuffer: invalid argument (cannot encode character '\187')` —
# measured, third probe build. The failure is in the OUTPUT path, not the
# compile, which is why it lands hundreds of packages in and looks like a
# dependency problem.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN git clone "${SV2V_REPO}" /sv2v \
 && git config --global --add safe.directory /sv2v \
 && cd /sv2v && git checkout ${SV2V_REF} \
 && stack build --copy-bins --local-bin-path /out \
 && test -x /out/sv2v

# The self-test is the point of building it: a converter that cannot convert is
# not a tool, and "the binary exists" has never been the question.
RUN printf 'module t;\n  logic [3:0] a;\n  always_comb a = 4%s1;\n  initial $display("SV2V_OK");\nendmodule\n' "'d" > /tmp/t.sv \
 && /out/sv2v /tmp/t.sv > /tmp/t.v \
 && grep -q "module t" /tmp/t.v \
 && ! grep -q "always_comb" /tmp/t.v \
 && echo "sv2v self-test: SystemVerilog always_comb lowered to Verilog"

FROM scratch AS artifact
COPY --from=build /out/sv2v /foss/tools/bin/sv2v
