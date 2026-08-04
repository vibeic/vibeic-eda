// vibeic-eda#81 — the page's gap count must partition on the ledger's `pin_kind`,
// never on a tool name.
//
// Same technique as `staleness.mjs` and for the same reason: the rule ships as
// JavaScript inside `build_page.py`, so a JS re-implementation here would test the
// re-implementation. The expressions under test are SLICED OUT of `build_page.py`
// and evaluated, so a future edit that reverts the rule fails this file.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

// The shipped block, verbatim — WHOLE, from the headline comment that opens the
// derivation to the next statement after it. The boundaries are deliberately the
// ones that existed BEFORE this fix too, so this file can be pointed at the
// pre-fix `build_page.py` and will evaluate whatever that version shipped rather
// than failing to find itself. A harness that only runs against the fixed source
// proves nothing about the direction of the fix.
const start = src.indexOf("// THE TWO NUMBERS THIS PAGE EXISTS FOR.");
const end = src.indexOf("const patchForks");
if (start < 0 || end < 0 || end < start) {
  console.error("could not slice the gap-count block out of build_page.py");
  process.exit(3);
}
const body = src.slice(start, end);

function run(LEDGERS) {
  // `typeof` guards, for the same reason: against a version that never declared
  // these the harness must report a WRONG NUMBER, not crash on a missing name.
  const f = new Function("LEDGERS", `${body}
    return {commitsBehind, forksBehind, behindUnknown,
            assertTools: typeof assertRows === "undefined"
                           ? null : assertRows.map(d=>d.tool),
            unrecorded: typeof kindUnrecorded === "undefined"
                           ? null : kindUnrecorded.map(d=>d.tool)};`);
  return f(LEDGERS);
}

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}\n           got  ${JSON.stringify(got)}\n           want ${JSON.stringify(want)}`);
}

// ── 1. THE MEASURED CASE, shrunk. The real ledger on 2026-08-04 read
//       39 across 7; the report read 21 across 6; the difference was one
//       contents assertion carrying 18.
const measured = [
  {tool: "open_pdks", behind_commits: 18, ahead: 12, pin_kind: "contents_assertion",
   dockerfile_arg: "OPEN_PDKS_VOLUME_CONTENTS_SHA"},
  {tool: "iverilog",  behind_commits: 12, ahead: 20, pin_kind: "pin"},
  {tool: "verilator", behind_commits: 4,  ahead: 10, pin_kind: "pin"},
  {tool: "OpenROAD",  behind_commits: 2,  ahead: 73, pin_kind: "pin"},
  {tool: "pyuvm",     behind_commits: 1,  ahead: 15, pin_kind: "pin"},
  {tool: "sby",       behind_commits: 1,  ahead: 22, pin_kind: "pin"},
  {tool: "slang",     behind_commits: 1,  ahead: 0,  pin_kind: "pin"},
  {tool: "yosys",     behind_commits: 0,  ahead: 59, pin_kind: "pin"},
];
const m = run(measured);
check("the measured ledger counts 21 across 6, not 39 across 7",
      [m.commitsBehind, m.forksBehind], [21, 6]);
check("…and the excluded row is still carried for rendering",
      m.assertTools, ["open_pdks"]);

// ── 2. NEGATIVE CONTROL A — a genuine build-input pin that is behind MUST count.
//       A fix that simply stopped counting would pass test 1 and fail here.
check("a real pin behind upstream still counts",
      run([{tool: "iverilog", behind_commits: 12, ahead: 0, pin_kind: "pin"}])
        .commitsBehind, 12);

// ── 3. NEGATIVE CONTROL B — the exclusion must key on the FIELD, not the NAME.
//       `open_pdks` classified as a pin must be counted; the page may not know
//       any tool by name.
check("a row named open_pdks that IS a pin still counts",
      run([{tool: "open_pdks", behind_commits: 18, ahead: 0, pin_kind: "pin",
            dockerfile_arg: "OPEN_PDKS_REF"}]).commitsBehind, 18);

// ── 4. NEGATIVE CONTROL C — a MISNAMED assertion. `pin_kinds` classifies an
//       assertion-named ARG that a fetch step reads as a PIN, and the row then
//       carries pin_kind="pin". The page must follow the field, not the suffix,
//       or the convention becomes an escape hatch from the sweep (#60 inverted).
check("an assertion-NAMED arg the build fetches at still counts",
      run([{tool: "sneak", behind_commits: 7, ahead: 0, pin_kind: "pin",
            dockerfile_arg: "SNEAK_VOLUME_CONTENTS_SHA"}]).commitsBehind, 7);

// ── 5. A NEW prebuilt artefact needs no edit here — it is excluded the morning
//       `discover_forks` first classifies it.
check("a brand-new contents assertion is excluded with no code change",
      run([{tool: "some_future_pdk", behind_commits: 400, ahead: 0,
            pin_kind: "contents_assertion",
            dockerfile_arg: "SOME_FUTURE_PDK_VOLUME_CONTENTS_SHA"},
           {tool: "yosys", behind_commits: 3, ahead: 0, pin_kind: "pin"}])
        .commitsBehind, 3);

// ── 6. UNMEASURABLE is still its own number, and an assertion does not inflate
//       it. `behind_commits: null` on a pin is "could not be run"; on an
//       assertion there is no question to leave open.
const u = run([{tool: "a", behind_commits: null, ahead: 0, pin_kind: "pin"},
               {tool: "b", behind_commits: null, ahead: 0,
                pin_kind: "contents_assertion"},
               {tool: "c", behind_commits: 5, ahead: 0, pin_kind: "pin"}]);
check("an unmeasurable PIN is +1?, an unmeasurable assertion is not",
      [u.commitsBehind, u.forksBehind, u.behindUnknown], [5, 1, 1]);

// ── 7. A row with NO recorded kind is COUNTED, never assumed clean — and named.
//       Dropping it would under-report; hiding it would re-create this defect
//       silently for the next artefact.
const k = run([{tool: "legacy", behind_commits: 9, ahead: 0},
               {tool: "yosys", behind_commits: 1, ahead: 0, pin_kind: "pin"}]);
check("an unclassified row is counted and named, not dropped",
      [k.commitsBehind, k.forksBehind, k.unrecorded], [10, 2, ["legacy"]]);

process.exit(bad ? 1 : 0);
