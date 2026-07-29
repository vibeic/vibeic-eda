# The build DAG. `docker buildx bake` reads this and builds only what moved.
#
# Two things live here that a plain `docker build` cannot express:
#
#   1. Each tool is its own target, so BuildKit schedules them as a graph — a
#      target starts when its own inputs are ready instead of waiting for the
#      level above to finish. This is the structure IIC-OSIC-TOOLS uses
#      upstream (`_build/docker-bake.hcl`); we had flattened it into one
#      605-line Dockerfile, and the flattening is what removed per-tool build
#      isolation.
#
#   2. `cache-from`/`cache-to` on every target. `release.yml` previously passed
#      NEITHER (measured: `grep -c "cache-from\|cache-to"` -> 0), so every
#      release recompiled all eight tools whether or not their pins had moved.
#
# TWO WAYS TO BUILD THE FINAL IMAGE, and the difference matters:
#
#   `bake eda`        composes from ghcr.io/vibeic/eda-tool-*:<sha> — what CI
#                     does. Nothing compiles; the pins in Dockerfile decide
#                     exactly which binaries land, and a pin that names an
#                     unbuilt image fails loudly rather than silently building
#                     something else.
#
#   `bake eda-local`  composes from tool targets built here and now. For
#                     working on a tool without pushing first. It does NOT
#                     prove what a release will contain — the pins are ignored
#                     — so a green `eda-local` is not evidence about a release.
#
# Adding a tool means: a `tools/<name>/Dockerfile`, a target below, an
# `IMG_<NAME>` pin in `Dockerfile`, and an entry in fork-gatekeeper's
# FORKS.json. `tools/check_fork_only.py` fails the build if its source is not
# ours.

variable "REGISTRY" { default = "ghcr.io/vibeic" }

# The release tag of the composed image. Per-tool tags are NOT this — they are
# the tool's own commit SHA, so an artefact is addressed by what produced it
# rather than by which release happened to ship it.
variable "TAG" { default = "dev" }

# One pin per tool: the commit of vibeic/<tool> that built the artefact.
# These are the ONLY place a tool version is stated. Keep them equal to the
# ARG defaults in Dockerfile — `tools/check_pins_agree.py` fails if they drift,
# because two sources of truth for a version means one of them is wrong and
# nobody can tell which.
variable "OPENROAD_REF"    { default = "92b079b47a1c1c470eb9fb0d32613a0b27379ad6" }
variable "YOSYS_REF"       { default = "b35f2c6d5867a6be5ae625cec835ee050b4d4580" }
variable "KISSAT_REF"      { default = "8af8e56f174b778aef3aa45af9f739b2a5f492c2" }
variable "CADICAL_REF"     { default = "c60730422e758ef1cebe7aeddf2dda31c996bf04" }
variable "NGSPICE_REF"     { default = "2d15ecb34c1b606cf653bafbdd21315b6bc21962" }
variable "MAGIC_REF"       { default = "9d3ed4b16b5e5d6570846b448b89ed7d953cd14b" }
variable "NETGEN_REF"      { default = "0334b7dfb1d6adce0a8079f5552f68982815d3d9" }
variable "IVERILOG_REF"    { default = "fe9dfabc4beb78f04bf9d7b9f52992b8d629ad8b" }
variable "KLAYOUT_REF"     { default = "39b6a09249a97b9739f46d9404018bbd69675751" }
variable "VERILATOR_REF"   { default = "d9f46707510f7d0ad67579aee755c2aabbf1230e" }

function "short" {
  params = [ref]
  result = substr(ref, 0, 7)
}

# A tool image is tagged with its own commit. Two releases pinning the same SHA
# resolve to the same image — which is what makes "did this tool change between
# these two releases" answerable by comparing one string.
function "tool_tags" {
  params = [name, ref]
  result = ["${REGISTRY}/eda-tool-${name}:${short(ref)}"]
}

target "_tool" {
  platforms  = ["linux/amd64"]
  cache-from = ["type=gha"]
  cache-to   = ["type=gha,mode=max"]
}

target "openroad" {
  inherits = ["_tool"]
  context  = "tools/openroad"
  args     = { OPENROAD_REF = OPENROAD_REF }
  tags     = tool_tags("openroad", OPENROAD_REF)
}

target "yosys" {
  inherits = ["_tool"]
  context  = "tools/yosys"
  args     = { YOSYS_REF = YOSYS_REF }
  tags     = tool_tags("yosys", YOSYS_REF)
}

# Two solvers, one image: both small, both from the same author, both consumed
# by the same yosys `smtbmc` path — splitting them would double the registry
# round-trips to save nothing.
#
# The tag carries BOTH commits. Keying it on KISSAT_REF alone would mean a
# CADICAL_REF bump left the tag unchanged, so the release would keep pulling the
# image built before the bump — a version change that silently does not ship.
target "sat-solvers" {
  inherits = ["_tool"]
  context  = "tools/sat-solvers"
  args     = { KISSAT_REF = KISSAT_REF, CADICAL_REF = CADICAL_REF }
  tags     = ["${REGISTRY}/eda-tool-sat-solvers:${short(KISSAT_REF)}-${short(CADICAL_REF)}"]
}

target "ngspice" {
  inherits = ["_tool"]
  context  = "tools/ngspice"
  args     = { NGSPICE_REF = NGSPICE_REF }
  tags     = tool_tags("ngspice", NGSPICE_REF)
}

# magic + netgen ship together: netgen's LVS only ever runs on magic's
# extraction, and the pair is what the `lvs-triage` path invokes. As with
# sat-solvers, the tag carries both commits so neither can move invisibly.
target "lvs" {
  inherits = ["_tool"]
  context  = "tools/lvs"
  args     = { MAGIC_REF = MAGIC_REF, NETGEN_REF = NETGEN_REF }
  tags     = ["${REGISTRY}/eda-tool-lvs:${short(MAGIC_REF)}-${short(NETGEN_REF)}"]
}

target "iverilog" {
  inherits = ["_tool"]
  context  = "tools/iverilog"
  args     = { IVERILOG_REF = IVERILOG_REF }
  tags     = tool_tags("iverilog", IVERILOG_REF)
}

target "klayout" {
  inherits = ["_tool"]
  context  = "tools/klayout"
  args     = { KLAYOUT_REF = KLAYOUT_REF }
  tags     = tool_tags("klayout", KLAYOUT_REF)
}

target "verilator" {
  inherits = ["_tool"]
  context  = "tools/verilator"
  args     = { VERILATOR_REF = VERILATOR_REF }
  tags     = tool_tags("verilator", VERILATOR_REF)
}

group "tools" {
  targets = ["openroad", "yosys", "sat-solvers", "ngspice",
             "lvs", "iverilog", "klayout", "verilator"]
}

# The release image. No `contexts` block and no dependency on the tool targets:
# it pulls the pinned artefacts from the registry, which is precisely what makes
# it reproducible — the inputs are images that already exist, not builds that
# might drift.
target "eda" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64"]
  tags       = ["${REGISTRY}/vibeic-eda:${TAG}", "${REGISTRY}/vibeic-eda:latest"]
  cache-from = ["type=gha"]
  cache-to   = ["type=gha,mode=max"]
}

# Local iteration. `contexts` redirects each `COPY --from=${IMG_X}` at the
# freshly built target, so a tool change is visible without a push.
#
# The pins in Dockerfile are IGNORED here. That is the point of the target and
# also its limit: a green `eda-local` says the composition works, never that a
# release built from the pins would contain the same binaries.
target "eda-local" {
  inherits = ["eda"]
  tags     = ["${REGISTRY}/vibeic-eda:local"]
  contexts = {
    "ghcr.io/vibeic/eda-tool-openroad:${short(OPENROAD_REF)}"      = "target:openroad"
    "ghcr.io/vibeic/eda-tool-yosys:${short(YOSYS_REF)}"            = "target:yosys"
    "ghcr.io/vibeic/eda-tool-sat-solvers:${short(KISSAT_REF)}-${short(CADICAL_REF)}" = "target:sat-solvers"
    "ghcr.io/vibeic/eda-tool-ngspice:${short(NGSPICE_REF)}"        = "target:ngspice"
    "ghcr.io/vibeic/eda-tool-lvs:${short(MAGIC_REF)}-${short(NETGEN_REF)}" = "target:lvs"
    "ghcr.io/vibeic/eda-tool-iverilog:${short(IVERILOG_REF)}"      = "target:iverilog"
    "ghcr.io/vibeic/eda-tool-klayout:${short(KLAYOUT_REF)}"        = "target:klayout"
    "ghcr.io/vibeic/eda-tool-verilator:${short(VERILATOR_REF)}"    = "target:verilator"
  }
}

group "default" { targets = ["eda"] }
