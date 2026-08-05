// Render the EMITTED page's own script and print what a viewer would see.
//
//     node jstests/render_page.mjs <page.html> [<pinned ISO now>]
//
// WHY THIS EXISTS AND WHY IT IS NOT `staleness.mjs`. Those harnesses extract an
// expression and evaluate it, which proves the RULE. It does not prove the rule
// reaches the page: a correct rule assigned to nothing, or rendered into an element
// the HTML does not contain, passes an expression test and shows a viewer nothing.
// This runs the shipped <script> against the shipped markup, so what it prints is
// what the browser would put on screen.
//
// The DOM shim is deliberately small — enough for the fragments this page assembles.
// An element the page asks for and this shim does not know is CREATED rather than
// returned as null, so a missing id fails as an empty render rather than a crash
// that would be mistaken for a bug in the harness.
import fs from "node:fs";

const html = fs.readFileSync(process.argv[2], "utf8");
const NOW = process.argv[3] ? Date.parse(process.argv[3]) : Date.now();
if (!isFinite(NOW)) { console.error("unparseable pinned now"); process.exit(2); }

// The page's own inline script — the first <script> with no src.
const m = html.match(/<script>\n([\s\S]*?)<\/script>/);
if (!m) { console.error("no inline <script> in the page"); process.exit(2); }
const script = m[1];

const els = new Map();
// Whether the SHIPPED markup starts this element hidden, rather than a default this
// harness invented. `forkStale` is `<div id="forkStale" hidden>` and the page unhides
// it; every other element starts visible, and a shim that hid them all would report
// a rendered fragment as invisible.
const startsHidden = (id) =>
  new RegExp(`id="${id}"[^>]*\\shidden(\\s|>|=)`).test(html);
const mkEl = (id) => {
  const el = {
    id, innerHTML: "", textContent: "", hidden: startsHidden(id), style: {}, dataset: {},
    className: "", attrs: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    appendChild(){}, addEventListener(){}, removeEventListener(){},
    setAttribute(k,v){ this.attrs[k]=v; }, getAttribute(k){ return this.attrs[k] ?? null; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    closest(){ return null; }, getBoundingClientRect(){ return {top:0,left:0,width:0,height:0}; },
  };
  return el;
};
const document = {
  getElementById(id) {
    if (!els.has(id)) els.set(id, mkEl(id));
    return els.get(id);
  },
  querySelector(){ return null },
  querySelectorAll(){ return [] },
  createElement(){ return mkEl("_new") },
  addEventListener(){}, body: mkEl("body"), documentElement: mkEl("html"),
};
const RealDate = Date;
class PinnedDate extends RealDate {
  constructor(...a){ super(...(a.length ? a : [NOW])); }
  static now(){ return NOW; }
}
PinnedDate.parse = RealDate.parse;
PinnedDate.UTC = RealDate.UTC;

const ctx = {
  document,
  Date: PinnedDate,
  localStorage: { getItem(){ return null }, setItem(){} },
  console,
  location: { hash: "", href: "https://vibeic.ai/eda-forks.html" },
  navigator: { language: "en" },
};
ctx.window = ctx;

let err = null;
try {
  new Function("window","document","Date","localStorage","console","location","navigator",
               script)(ctx, document, PinnedDate, ctx.localStorage, console, ctx.location, ctx.navigator);
} catch (e) { err = e; }

const want = process.env.RENDER_IDS ? process.env.RENDER_IDS.split(",") : ["forkMetrics","forkStale","forkGap"];
for (const id of want) {
  const el = els.get(id);
  console.log(`===== #${id} =====`);
  console.log(el ? (el.hidden ? "[hidden] " : "") + el.innerHTML : "[the page never asked for this element]");
}
if (err) { console.log("===== script error ====="); console.log(String(err && err.stack || err)); }
process.exit(err ? 1 : 0);
