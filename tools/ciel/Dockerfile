# eda-tool-ciel — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-ciel:<ref>-<recipe>`. The tag IS the pin.
#
# vibeic-eda#60: before this, the image took ciel from the BASE image and our
# fork could not reach a user. ciel is the PDK version manager — it is what
# materialises /foss/pdks/sky130A and gf180mcuD (both are symlinks into
# /foss/pdks/ciel/<family>/versions/<open_pdks-sha>/), so a patch of ours to how
# a PDK is resolved or pinned had nowhere to go.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29).

FROM ubuntu:24.04 AS build
ARG CIEL_REPO=https://github.com/vibeic/ciel.git
ARG CIEL_REF=d2af9cd676810db6ac7e15895a3285bf7d06034c  # pinned; branch main — identical to fossi-foundation/ciel@main today
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*
# A venv, not --break-system-packages: the runtime stage copies a self-contained
# tree, and PEP 668 makes the system interpreter refuse anyway.
RUN python3 -m venv /opt/ciel \
 && git clone "${CIEL_REPO}" /src/ciel \
 && git config --global --add safe.directory /src/ciel \
 && cd /src/ciel && git checkout ${CIEL_REF} \
 && /opt/ciel/bin/pip install --no-cache-dir . \
 && test -x /opt/ciel/bin/ciel

# The self-test: a version manager that cannot enumerate PDK families is not one.
RUN /opt/ciel/bin/ciel --version \
 && /opt/ciel/bin/ciel ls-pdks 2>&1 | tee /tmp/pdks.txt \
 && grep -qi "sky130" /tmp/pdks.txt \
 && echo "ciel self-test: enumerates PDK families including sky130"

RUN mkdir -p /vibeic/provenance \
 && source_ref=$(git -C /src/ciel rev-parse HEAD) \
 && test "$source_ref" = "${CIEL_REF}" \
 && printf '{"tool":"ciel","repo":"%s","ref":"%s"}\n' \
      "$(git -C /src/ciel remote get-url origin)" "$source_ref" \
      > /vibeic/provenance/ciel.json

FROM scratch AS artifact
COPY --from=build /opt/ciel /opt/ciel
COPY --from=build /vibeic/provenance/ciel.json /vibeic/provenance/ciel.json
