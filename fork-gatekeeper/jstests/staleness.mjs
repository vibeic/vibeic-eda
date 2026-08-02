import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
// Extract the SHIPPED expressions rather than retyping them: a test that
// re-derives the rule proves the re-derivation.
const seg = src.slice(src.indexOf("const genMs = Date.parse(generatedAt);"));
const body = seg.slice(0, seg.indexOf("const ageTxt")) ;
const ageTxtLine = seg.slice(seg.indexOf("const ageTxt"));
const ageTxtSrc = ageTxtLine.slice(0, ageTxtLine.indexOf(";") + 1);
const NOW = Date.parse("2026-08-03T05:30:00+08:00");
function run(generatedAt) {
  const Date_ = { parse: Date.parse, now: () => NOW };
  const f = new Function("generatedAt", "Date", `${body}${ageTxtSrc}
    return {stale, ahead, ageH, ageTxt, readable: isFinite(genMs)};`);
  return f(generatedAt, Date_);
}
const cases = [
  ["2026-08-03T05:00:00+08:00", false, "half an hour old — a round that just ran"],
  ["2026-08-02T05:30:00+08:00", false, "24 h — yesterday's round, today's not due yet"],
  ["2026-08-01T22:51:45+08:00", true,  "the incident's frozen ledger, next morning"],
  ["2026-07-28T05:30:00+08:00", true,  "six days"],
];
let bad = 0;
for (const [ts, want, why] of cases) {
  const r = run(ts);
  const ok = r.stale === want;
  if (!ok) bad++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${ts}  stale=${r.stale} age=${r.ageH.toFixed(1)}h (${r.ageTxt})  — ${why}`);
}
const fut = run("2026-08-09T00:00:00+08:00");
console.log(`  ${fut.ahead && !fut.stale ? "ok  " : "FAIL"}  future stamp -> ahead=${fut.ahead} stale=${fut.stale}  — must not read as fresh`);
if (!(fut.ahead && !fut.stale)) bad++;
const junk = run("not a date");
console.log(`  ${!junk.readable && !junk.stale ? "ok  " : "FAIL"}  unparseable -> readable=${junk.readable}  — must not read as fresh`);
if (junk.readable) bad++;
process.exit(bad ? 1 : 0);
