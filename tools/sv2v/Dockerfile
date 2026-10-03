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
ARG SV2V_REF=493a88f930802da68f592c192ec86991eaf95c49  # pinned; branch master — identical to zachjs/sv2v@master today (ahead=0, behind=0)
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
# Stack 2.9.3's bootstrap keys predate Hackage's root-key rotation. Configure
# the supported package-index override with the current authenticated root set;
# retain the three-signature threshold and enforce metadata expiry.
# Source: haskell-infra/hackage-root-keys@68ed020253c23517e3b10b54876d8aba93d4c15c/root.json
# https://docs.haskellstack.org/en/latest/configure/yaml/non-project/#package-index
RUN mkdir -p /etc/stack \
 && printf '%s\n' \
      'package-index:' \
      '  download-prefix: https://hackage.haskell.org/' \
      '  hackage-security:' \
      '    keyids:' \
      '    - 0a5c7ea47cd1b15f01f5f51a33adda7e655bc0f0b0615baa8e271f4c3351e21d' \
      '    - 1ea9ba32c526d1cc91ab5e5bd364ec5e9e8cb67179a471872f6e26f0ae773d42' \
      '    - 51f0161b906011b52c6613376b1ae937670da69322113a246a09f807c62f6921' \
      '    - c7de58fc6a224b92b5b513f26fbb8b370f2d97c7cfe0075a951314a55734be93' \
      '    - fe331502606802feac15e514d9b9ea83fee8b6ffef71335479a2e68d84adc6b0' \
      '    - d26e46f3b631aae1433b89379a6c68bd417eb5d1c408f0643dcc07757fece522' \
      '    key-threshold: 3' \
      '    ignore-expiry: false' \
      > /etc/stack/config.yaml
# Authenticate the package index before downloading GHC. The tool-local cache
# survives a failed build, while Stack still verifies downloaded metadata and
# package hashes; the compiler/dependency cache is never a shipped artifact.
RUN --mount=type=cache,id=vibeic-sv2v-stack-ubuntu24,target=/root/.stack,sharing=locked \
    stack update
RUN git clone "${SV2V_REPO}" /sv2v \
 && git config --global --add safe.directory /sv2v \
 && cd /sv2v && git checkout ${SV2V_REF}
RUN --mount=type=cache,id=vibeic-sv2v-stack-ubuntu24,target=/root/.stack,sharing=locked \
    cd /sv2v \
 && stack build --copy-bins --local-bin-path /out \
 && test -x /out/sv2v

# The self-test is the point of building it: a converter that cannot convert is
# not a tool, and "the binary exists" has never been the question.
RUN printf 'module t;\n  logic [3:0] a;\n  always_comb a = 4%s1;\n  initial $display("SV2V_OK");\nendmodule\n' "'d" > /tmp/t.sv \
 && /out/sv2v /tmp/t.sv > /tmp/t.v \
 && grep -q "module t" /tmp/t.v \
 && ! grep -q "always_comb" /tmp/t.v \
 && echo "sv2v self-test: SystemVerilog always_comb lowered to Verilog"

RUN mkdir -p /vibeic/provenance \
 && source_ref=$(git -C /sv2v rev-parse HEAD) \
 && test "$source_ref" = "${SV2V_REF}" \
 && printf '{"tool":"sv2v","repo":"%s","ref":"%s"}\n' \
      "$(git -C /sv2v remote get-url origin)" "$source_ref" \
      > /vibeic/provenance/sv2v.json

FROM scratch AS artifact
COPY --from=build /out/sv2v /foss/tools/bin/sv2v
COPY --from=build /vibeic/provenance/sv2v.json /vibeic/provenance/sv2v.json
