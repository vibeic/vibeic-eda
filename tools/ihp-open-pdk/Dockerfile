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
ARG IHP_OPEN_PDK_REF=5e6d592e4002946a4616f798c357f0f3c06cf3b6  # pinned; branch main — identical to IHP-GmbH/IHP-Open-PDK@main today
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none "${IHP_OPEN_PDK_REPO}" /src/ihp \
 && git config --global --add safe.directory /src/ihp \
 && cd /src/ihp && git checkout ${IHP_OPEN_PDK_REF} \
 && test -d /src/ihp/ihp-sg13g2

# The SECOND family, from its OWN repository. vibeic-eda#176: ihp-sg13cmos5l
# used to arrive pre-installed in the base image at a commit from 2026-06-18,
# while the sg13g2 above is from 2026-09-01. cmos5l's PCell library reaches
# into sg13g2 by relative symlink for its shared modules, and two of the
# targets (guard_ring_code.py, device_base_code.py) were dropped from BOTH
# families upstream in between -- so the base's older cmos5l dangled into our
# newer sg13g2 and KLayout logged `ERROR: Reading ...` on every load. The two
# families are one PDK in two repositories; they are pinned together here so
# they cannot drift apart again. Same repository rule: a vibeic mirror.
ARG IHP_SG13CMOS5L_REPO=https://github.com/vibeic/ihp-sg13cmos5l.git
ARG IHP_SG13CMOS5L_REF=597570eec556a473d1eedb22c055545d4522f442  # pinned; branch main — identical to IHP-GmbH/ihp-sg13cmos5l@main today
RUN git clone --filter=blob:none "${IHP_SG13CMOS5L_REPO}" /src/ihp-sg13cmos5l \
 && git config --global --add safe.directory /src/ihp-sg13cmos5l \
 && cd /src/ihp-sg13cmos5l && git checkout ${IHP_SG13CMOS5L_REF} \
 && test -d /src/ihp-sg13cmos5l/libs.tech

# PDKS.json declares ihp-sg13g2's version_file as COMMIT (40 hex chars), the
# convention the base image's pre-baked copy shipped under before #60 moved
# this PDK to our own build. A git checkout carries no such file on its own
# (verified: neither vibeic/IHP-Open-PDK nor upstream ships one) -- write it
# from the ref we actually pinned, or the next change to this PDK is
# undetectable from outside the image (vibeic-eda claims-gate finding, 0.2.68).
RUN printf '%s' "${IHP_OPEN_PDK_REF}" > /src/ihp/ihp-sg13g2/COMMIT \
 && grep -qE '^[0-9a-f]{40}$' /src/ihp/ihp-sg13g2/COMMIT
RUN printf '%s' "${IHP_SG13CMOS5L_REF}" > /src/ihp-sg13cmos5l/COMMIT \
 && grep -qE '^[0-9a-f]{40}$' /src/ihp-sg13cmos5l/COMMIT

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

# The cmos5l self-test asks the same question, plus the one that failed:
# every symlink its tree makes into sg13g2 must resolve against the sg13g2
# checked out ABOVE, in this same stage, laid out as the image lays them out
# (siblings under one directory, so `../../../../ihp-sg13g2/...` means the
# pinned one). A dangling link into SOURCE is a build failure, not a runtime
# `ERROR: Reading ...` line. Links to BUILD PRODUCTS (`*.osdi`, compiled by the
# composing Dockerfile; `*.so`, Xyce plugins nobody builds for this family) are
# counted and reported but not judged here: they cannot exist in a data stage.
RUN ln -s /src/ihp/ihp-sg13g2 /src/ihp-sg13g2 \
 && cd /src/ihp-sg13cmos5l \
 && test -d libs.tech && test -d libs.ref \
 && find libs.ref -name '*.lef' | head -1 | grep -q . \
 && find libs.tech -name '*.lyp' -o -name '*.drc' -o -name '*.lydrc' | head -1 | grep -q . \
 && find . -type l ! -name '*.osdi' ! -name '*.so' ! -exec test -e {} \; -print > /tmp/dangling.txt \
 && dangling=$(wc -l < /tmp/dangling.txt) \
 && products=$(find . -type l \( -name '*.osdi' -o -name '*.so' \) ! -exec test -e {} \; -print | wc -l) \
 && { [ "$dangling" = "0" ] || { echo "FAIL: $dangling symlink(s) in ihp-sg13cmos5l point at ihp-sg13g2 SOURCE that the pinned ihp-sg13g2 does not carry:"; cat /tmp/dangling.txt; exit 1; }; } \
 && rm -f /tmp/dangling.txt /src/ihp-sg13g2 \
 && echo "ihp-sg13cmos5l self-test: libs.tech + libs.ref present, LEF and a KLayout deck resolve, 0 dangling source symlinks into ihp-sg13g2 ($products link(s) to build products, resolved or not by later stages)"

FROM scratch AS artifact
COPY --from=build /src/ihp/ihp-sg13g2 /foss/pdks/ihp-sg13g2
COPY --from=build /src/ihp-sg13cmos5l /foss/pdks/ihp-sg13cmos5l
