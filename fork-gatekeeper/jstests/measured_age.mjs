// vibeic-eda#91 — the per-row measurement-age rule, evaluated as SHIPPED.
//
// Same method as staleness.mjs and for the same reason: the rule is JavaScript in
// the emitted page, so a Python re-implementation would test the re-implementation.
// This EXTRACTS the shipped expressions out of build_page.py and runs those, with
// Date.now pinned so the cases are deterministic.
import fs from "node:fs";

const src = fs.readFileSync(process.argv[2], "utf8");

// From the threshold constant through the bilingual clause — one contiguous slice of
// the shipped source, so nothing between them can be quietly different here.
const from = src.indexOf("const STALE_HOURS = 30;");
if (from < 0) { console.log("FAIL  could not find `const STALE_HOURS` in the page source"); process.exit(1); }
const tail = src.slice(from);
const end = tail.indexOf("const ageSpan");
if (end < 0) { console.log("FAIL  could not find `const ageSpan` in the page source"); process.exit(1); }
const body = tail.slice(0, end);

const NOW = Date.parse("2026-08-05T09:00:00+08:00");

function run(row) {
  const Date_ = { parse: Date.parse, now: () => NOW };
  const f = new Function("d", "Date", `${body}
    return {state: measuredState(d), ageH: measuredAgeH(d), clause: ageClause(d),
            clauseZh: ageClauseZh(d)};`);
  return f(row, Date_);
}

let bad = 0;
function check(ok, line) { if (!ok) bad++; console.log(`  ${ok ? "ok  " : "FAIL"}  ${line}`); }

// ---- 1. FRESH: measured this morning's round -------------------------------------
{
  const r = run({tool: "iverilog", behind_commits: 12,
                 behind_measured_at: "2026-08-05T05:31:00+08:00"});
  check(r.state === "fresh", `fresh round (3.5 h) -> state=${r.state}`);
  check(/measured 3 h ago/.test(r.clause) && !/STALE/.test(r.clause),
        `fresh clause states an age and does NOT say STALE -> "${r.clause.trim()}"`);
}
// A count of ZERO is a measurement like any other and must be aged the same way.
{
  const r = run({tool: "iverilog", behind_commits: 0,
                 behind_measured_at: "2026-08-05T05:31:00+08:00"});
  check(r.state === "fresh", `zero is a measurement, not an absence -> state=${r.state}`);
}

// ---- 2. STALE: past one daily round + slack ---------------------------------------
// 29 h is INSIDE the window, 31 h is outside — the boundary is asserted from both
// sides, so a threshold silently widened to 48 h would fail here rather than pass.
{
  const r = run({behind_commits: 12, behind_measured_at: "2026-08-04T04:00:00+08:00"});
  check(r.state === "fresh", `29 h — inside one round plus slack -> state=${r.state}`);
}
{
  const r = run({behind_commits: 12, behind_measured_at: "2026-08-04T02:00:00+08:00"});
  check(r.state === "stale", `31 h — a round that should have replaced it did not -> state=${r.state}`);
  check(/STALE/.test(r.clause), `stale clause says STALE -> "${r.clause.trim()}"`);
}
// THE ISSUE'S OWN NUMBERS. iverilog's ledger said 12 while live was 0; the ledger was
// written 2026-08-04T18:14 and the page was read the next day. That row must not print
// as current.
{
  const r = run({tool: "iverilog", behind_commits: 12,
                 behind_measured_at: "2026-08-04T18:14:00+08:00"});
  check(r.state === "fresh", `#91's iverilog row read 14.8 h later — still inside the window`);
  const later = new Function("d", "Date", `${body}
    return measuredState(d);`)({tool: "iverilog", behind_commits: 12,
                                behind_measured_at: "2026-08-04T18:14:00+08:00"},
                               {parse: Date.parse, now: () => Date.parse("2026-08-06T05:00:00+08:00")});
  check(later === "stale", `#91's iverilog row, read on the morning AFTER the next round should have run -> ${later}`);
}

// ---- 3. UNKNOWN-AGE: the migration case, and it is the COMMON one -----------------
// Every ledger on disk the day this ships has no such field. It must be its own state
// — never "fresh", and never "stale" either, because we do not know that it is.
{
  const r = run({tool: "verilator", behind_commits: 4});
  check(r.state === "unknown-age", `no field at all (every existing ledger) -> state=${r.state}`);
  check(/UNKNOWN-AGE/.test(r.clause), `says UNKNOWN-AGE -> "${r.clause.trim()}"`);
  check(!/measured \d+ h ago/.test(r.clause), `does NOT state an age it does not have`);
  check(/年齡不明/.test(r.clauseZh), `and says so in Chinese too -> "${r.clauseZh.trim()}"`);
}
{
  const r = run({behind_commits: 4, behind_measured_at: null});
  check(r.state === "unknown-age", `explicit null -> state=${r.state}`);
}
{
  const r = run({behind_commits: 4, behind_measured_at: "not a date"});
  check(r.state === "unknown-age", `unparseable -> state=${r.state}`);
}
// Clock skew. A stamp in the FUTURE cannot be aged, and the one direction that must
// never read as healthy is "fresh" — same rule the page banner already applies.
{
  const r = run({behind_commits: 4, behind_measured_at: "2026-08-09T00:00:00+08:00"});
  check(r.state === "unknown-age", `future stamp -> state=${r.state}, must not read as fresh`);
}

// ---- 4. NOT-MEASURED is not an age question --------------------------------------
// A row with no count has nothing for an age to qualify; the page already reports it
// as `+N?` beside the headline. It must not be mixed into the stale/unknown buckets.
{
  const r = run({tool: "Fault", behind_commits: null, compare_error: "404"});
  check(r.state === "not-measured", `no count -> state=${r.state}`);
  check(r.clause === "", `and no age clause at all`);
}

// ---- 5. THE FALLBACK THAT MUST NOT EXIST -----------------------------------------
// `generated_at` is stamped before anything is measured and survives every early
// return. If the rule ever falls back to it, a row that measured NOTHING would render
// as measured seconds ago. This is the assertion that catches that edit.
{
  const r = run({behind_commits: 4, generated_at: "2026-08-05T08:59:00+08:00"});
  check(r.state === "unknown-age",
        `a fresh generated_at does NOT make an unstamped count fresh -> state=${r.state}`);
}

process.exit(bad ? 1 : 0);
