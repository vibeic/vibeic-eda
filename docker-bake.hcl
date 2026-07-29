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

# The RECIPE half of an artefact's identity: a digest of the tool's own
# Dockerfile. Written by fork-gatekeeper/daily_release.py, exactly as the *_REF
# pins above are. It exists as a bake VARIABLE rather than living only in that
# program because two places compose a tool tag — `tool_tags` and `eda-local`'s
# `contexts` map — and when only the program knew about the recipe those two
# stopped agreeing, which silently disabled the local-build redirect (#21).
variable "OPENROAD_RECIPE" { default = "7ac820" }
variable "YOSYS_RECIPE" { default = "74a892" }
variable "SAT_SOLVERS_RECIPE" { default = "755999" }
variable "NGSPICE_RECIPE" { default = "be7db2" }
variable "LVS_RECIPE" { default = "e2e322" }
variable "IVERILOG_RECIPE" { default = "940079" }
variable "KLAYOUT_RECIPE" { default = "7cb6ee" }
variable "VERILATOR_RECIPE" { default = "8c0ab6" }
variable "GTKWAVE_RECIPE" { default = "2166b3" }
variable "XSCHEM_RECIPE" { default = "382491" }
variable "SLANG_RECIPE" { default = "b569b8" }
variable "XYCE_RECIPE" { default = "9d3df7" }
variable "YICES2_RECIPE" { default = "04c594" }
variable "SV_ELAB_RECIPE" { default = "0656db" }


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
variable "GTKWAVE_REF" { default = "7d7b4db9e2f5485afe2aeeab0ad112f5b6a9b94b" }
variable "XSCHEM_REF" { default = "c8b26a17d8d53ce7fbd9e7d45ab6bb03e75996e0" }
variable "SLANG_REF" { default = "24809c8e0b94d0915fe05b44d98c2df7a8e80c3e" }
variable "XYCE_REF" { default = "a592a42ae7472151d1c8e3bca0ca62d27476f2f3" }
variable "YICES2_REF" { default = "05178c03ddf49c6bba63c5c7153774c11a5da12d" }
variable "SV_ELAB_REF" { default = "b2b718c5a66ad525858298466f7ecaa60497393e" }

function "short" {
  params = [ref]
  result = substr(ref, 0, 7)
}

# A tool image is tagged with its own commit. Two releases pinning the same SHA
# resolve to the same image — which is what makes "did this tool change between
# these two releases" answerable by comparing one string.
function "tool_tags" {
  params = [name, ref, recipe]
  result = ["${REGISTRY}/eda-tool-${name}:${short(ref)}-${recipe}"]
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
  tags     = tool_tags("openroad", OPENROAD_REF, OPENROAD_RECIPE)
}

target "yosys" {
  inherits = ["_tool"]
  context  = "tools/yosys"
  args     = { YOSYS_REF = YOSYS_REF }
  tags     = tool_tags("yosys", YOSYS_REF, YOSYS_RECIPE)
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
  tags     = ["${REGISTRY}/eda-tool-sat-solvers:${short(KISSAT_REF)}-${short(CADICAL_REF)}-${SAT_SOLVERS_RECIPE}"]
}

target "ngspice" {
  inherits = ["_tool"]
  context  = "tools/ngspice"
  args     = { NGSPICE_REF = NGSPICE_REF }
  tags     = tool_tags("ngspice", NGSPICE_REF, NGSPICE_RECIPE)
}

# magic + netgen ship together: netgen's LVS only ever runs on magic's
# extraction, and the pair is what the `lvs-triage` path invokes. As with
# sat-solvers, the tag carries both commits so neither can move invisibly.
target "lvs" {
  inherits = ["_tool"]
  context  = "tools/lvs"
  args     = { MAGIC_REF = MAGIC_REF, NETGEN_REF = NETGEN_REF }
  tags     = ["${REGISTRY}/eda-tool-lvs:${short(MAGIC_REF)}-${short(NETGEN_REF)}-${LVS_RECIPE}"]
}

target "iverilog" {
  inherits = ["_tool"]
  context  = "tools/iverilog"
  args     = { IVERILOG_REF = IVERILOG_REF }
  tags     = tool_tags("iverilog", IVERILOG_REF, IVERILOG_RECIPE)
}

target "klayout" {
  inherits = ["_tool"]
  context  = "tools/klayout"
  args     = { KLAYOUT_REF = KLAYOUT_REF }
  tags     = tool_tags("klayout", KLAYOUT_REF, KLAYOUT_RECIPE)
}

target "verilator" {
  inherits = ["_tool"]
  context  = "tools/verilator"
  args     = { VERILATOR_REF = VERILATOR_REF }
  tags     = tool_tags("verilator", VERILATOR_REF, VERILATOR_RECIPE)
}

target "gtkwave" {
  inherits = ["_tool"]
  context  = "tools/gtkwave"
  args     = { GTKWAVE_REF = GTKWAVE_REF }
  tags     = tool_tags("gtkwave", GTKWAVE_REF, GTKWAVE_RECIPE)
}

target "xschem" {
  inherits = ["_tool"]
  context  = "tools/xschem"
  args     = { XSCHEM_REF = XSCHEM_REF }
  tags     = tool_tags("xschem", XSCHEM_REF, XSCHEM_RECIPE)
}

target "slang" {
  inherits = ["_tool"]
  context  = "tools/slang"
  args     = { SLANG_REF = SLANG_REF }
  tags     = tool_tags("slang", SLANG_REF, SLANG_RECIPE)
}

target "xyce" {
  inherits = ["_tool"]
  context  = "tools/xyce"
  args     = { XYCE_REF = XYCE_REF }
  tags     = tool_tags("xyce", XYCE_REF, XYCE_RECIPE)
}

target "yices2" {
  inherits = ["_tool"]
  context  = "tools/yices2"
  args     = { YICES2_REF = YICES2_REF }
  tags     = tool_tags("yices2", YICES2_REF, YICES2_RECIPE)
}

# sv-elab links against yosys' ABI, so it needs the yosys artefact as an input.
# `contexts` gives it the same tag the composing Dockerfile pulls, so a local
# `bake sv-elab` uses the yosys that was actually built rather than a registry
# copy that may not exist yet.
target "sv-elab" {
  inherits = ["_tool"]
  context  = "tools/sv-elab"
  args     = { SV_ELAB_REF = SV_ELAB_REF }
  contexts = { "ghcr.io/vibeic/eda-tool-yosys:${short(YOSYS_REF)}-${YOSYS_RECIPE}" = "target:yosys" }
  tags     = tool_tags("sv-elab", SV_ELAB_REF, SV_ELAB_RECIPE)
}

group "tools" {
  targets = ["openroad", "yosys", "sat-solvers", "ngspice",
             "lvs", "iverilog", "klayout", "verilator",
             "gtkwave", "xschem", "slang", "xyce",
             "yices2", "sv-elab"]
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
    "ghcr.io/vibeic/eda-tool-openroad:${short(OPENROAD_REF)}-${OPENROAD_RECIPE}"      = "target:openroad"
    "ghcr.io/vibeic/eda-tool-yosys:${short(YOSYS_REF)}-${YOSYS_RECIPE}"            = "target:yosys"
    "ghcr.io/vibeic/eda-tool-sat-solvers:${short(KISSAT_REF)}-${short(CADICAL_REF)}-${SAT_SOLVERS_RECIPE}" = "target:sat-solvers"
    "ghcr.io/vibeic/eda-tool-ngspice:${short(NGSPICE_REF)}-${NGSPICE_RECIPE}"        = "target:ngspice"
    "ghcr.io/vibeic/eda-tool-lvs:${short(MAGIC_REF)}-${short(NETGEN_REF)}-${LVS_RECIPE}" = "target:lvs"
    "ghcr.io/vibeic/eda-tool-iverilog:${short(IVERILOG_REF)}-${IVERILOG_RECIPE}"      = "target:iverilog"
    "ghcr.io/vibeic/eda-tool-klayout:${short(KLAYOUT_REF)}-${KLAYOUT_RECIPE}"        = "target:klayout"
    "ghcr.io/vibeic/eda-tool-verilator:${short(VERILATOR_REF)}-${VERILATOR_RECIPE}"    = "target:verilator"
    "ghcr.io/vibeic/eda-tool-gtkwave:${short(GTKWAVE_REF)}-${GTKWAVE_RECIPE}" = "target:gtkwave"
    "ghcr.io/vibeic/eda-tool-xschem:${short(XSCHEM_REF)}-${XSCHEM_RECIPE}" = "target:xschem"
    "ghcr.io/vibeic/eda-tool-slang:${short(SLANG_REF)}-${SLANG_RECIPE}" = "target:slang"
    "ghcr.io/vibeic/eda-tool-xyce:${short(XYCE_REF)}-${XYCE_RECIPE}" = "target:xyce"
    "ghcr.io/vibeic/eda-tool-yices2:${short(YICES2_REF)}-${YICES2_RECIPE}" = "target:yices2"
    "ghcr.io/vibeic/eda-tool-sv-elab:${short(SV_ELAB_REF)}-${SV_ELAB_RECIPE}" = "target:sv-elab"
  }
}

group "default" { targets = ["eda"] }
