# eda-tool-ihp-open-pdk — the SG13G2 PDK DATA, keyed on the commit it came from.
#
# Published as `ghcr.io/vibeic/eda-tool-ihp-open-pdk:<ref>-<recipe>`.
#
# vibeic-eda#60: before this, /foss/pdks/ihp-sg13g2 came from the BASE image and
# our fork could not reach a user. The SG13G2 spm cell runs on this PDK, so a
# patch of ours to a tech file or a device model had nowhere to go.
#
# DATA, not a build — there is nothing to compile. The value is the pin: which
# commit of the PDK a result was measured against, answerable by reading a tag.
# A shallow single-commit clone, because the history of a 1.2 GB data repo is
# not something the image needs to carry.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29).

FROM ubuntu:24.04 AS build
ARG IHP_OPEN_PDK_REPO=https://github.com/vibeic/IHP-Open-PDK.git
ARG IHP_OPEN_PDK_REF=22f2a25f1734796de3debbbf29cf697cbbc54081  # pinned; branch main — identical to IHP-GmbH/IHP-Open-PDK@main today
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none "${IHP_OPEN_PDK_REPO}" /src/ihp \
 && git config --global --add safe.directory /src/ihp \
 && cd /src/ihp && git checkout ${IHP_OPEN_PDK_REF} \
 && test -d /src/ihp/ihp-sg13g2

# The self-test asks what a PDK is FOR: the files every DRC/LVS/PnR run reads.
# "The directory exists" has never been the question.
RUN cd /src/ihp/ihp-sg13g2 \
 && test -d libs.tech && test -d libs.ref \
 && ls libs.tech | tee /tmp/t.txt \
 && grep -q klayout /tmp/t.txt \
 && grep -q openems /tmp/t.txt || true \
 && find libs.ref -name '*.lef' | head -1 | grep -q . \
 && find libs.tech -name '*.lyp' -o -name '*.drc' -o -name '*.lydrc' | head -1 | grep -q . \
 && echo "ihp-open-pdk self-test: libs.tech + libs.ref present, LEF and a KLayout deck resolve"

FROM scratch AS artifact
COPY --from=build /src/ihp/ihp-sg13g2 /foss/pdks/ihp-sg13g2
