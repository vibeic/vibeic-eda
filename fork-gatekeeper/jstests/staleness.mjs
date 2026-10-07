// Execute the emitted page, not a reimplementation of its display contract.
// The last completed snapshot must not become a warning page when the browser
// clock crosses 30 hours, a week, or is skewed relative to the server.
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";

const runner = fileURLToPath(new URL("./render_page.mjs", import.meta.url));
const page = process.argv[2];
const render = now => {
  const cp = spawnSync(process.execPath, [runner, page, now], {encoding: "utf8"});
  assert.equal(cp.status, 0, cp.stdout + cp.stderr);
  return cp.stdout;
};
const snapshot = render("2026-08-05T09:00:00+08:00");
assert.match(snapshot, /2026-08-05 08:30/);
assert.match(snapshot, /<b>7<\/b>/);
for (const now of ["2026-08-06T16:00:00+08:00", "2026-08-12T09:00:00+08:00",
                   "2026-08-01T09:00:00+08:00"]) {
  assert.equal(render(now), snapshot, `snapshot changed at browser time ${now}`);
}
assert.doesNotMatch(snapshot, /\d+ [hd] old|are STALE|UNKNOWN-AGE|is-stale|fork-age/);
console.log("PASS: emitted snapshot stays fixed across 31 h, 7 d and clock skew");
