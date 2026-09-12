/**
 * Pure logic behind the in-app review/feedback widget (FB-01, FB-03).
 *
 * Regression lines:
 * - if a stored panel position off the current viewport restores off-screen
 *   then the panel is unreachable and looks vanished
 * - if localStorage junk crashes parsePanelPos then the widget dies on mount
 * - if two keystrokes inside the debounce window issue two saves then typing
 *   hammers the daemon per character
 * - if flush() drops a pending value then feedback typed just before pagehide
 *   is lost, which the whole feature exists to prevent
 * - if a corner click maps outside 0..100 percent then the daemon 422s a
 *   legitimate pin
 * - if an anonymous div chain fabricates an anchor then harvested pins point
 *   agents at selectors that do not exist
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, test } from "node:test";
import { fileURLToPath } from "node:url";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let fb;
before(async () => {
  fb = await loadTypeScriptModule("src/lib/rb/feedback.ts");
});

// ----- clampPanelPos ------------------------------------------------------
test("a position past the right/bottom edge clamps fully on-viewport", () => {
  const clamped = fb.clampPanelPos(
    { x: 5000, y: 5000 },
    { w: 272, h: 300 },
    { w: 1280, h: 800 },
  );
  assert.deepEqual(clamped, { x: 1280 - 272 - 8, y: 800 - 300 - 8 });
});

test("a negative position clamps to the viewport margin", () => {
  const clamped = fb.clampPanelPos(
    { x: -50, y: -9 },
    { w: 272, h: 300 },
    { w: 1280, h: 800 },
  );
  assert.deepEqual(clamped, { x: 8, y: 8 });
});

test("a panel wider than the viewport pins to the margin rather than negative", () => {
  const clamped = fb.clampPanelPos(
    { x: 100, y: 10 },
    { w: 500, h: 300 },
    { w: 400, h: 800 },
  );
  assert.equal(clamped.x, 8);
});

// ----- parse/serialize panel pos -----------------------------------------
test("serialize -> parse round-trips a position", () => {
  const parsed = fb.parsePanelPos(fb.serializePanelPos({ x: 12.7, y: 34.2 }));
  assert.deepEqual(parsed, { x: 13, y: 34 });
});

test("garbage, null, partial and non-finite stored positions read as null", () => {
  for (const raw of [
    null,
    "not json",
    "{}",
    '{"x":1}',
    "[1,2]",
    '{"x":"a","y":2}',
    '{"x":null,"y":2}',
  ]) {
    assert.equal(fb.parsePanelPos(raw), null, `raw=${raw}`);
  }
  assert.equal(fb.parsePanelPos('{"x":1e999,"y":2}'), null, "Infinity refused");
});

// ----- makeDebounce -------------------------------------------------------
function manualTimers() {
  const pending = new Map();
  let seq = 0;
  return {
    schedule: (fn) => {
      seq += 1;
      pending.set(seq, fn);
      return seq;
    },
    cancel: (handle) => pending.delete(handle),
    fire() {
      const fns = [...pending.values()];
      pending.clear();
      for (const fn of fns) fn();
    },
    count: () => pending.size,
  };
}

test("debounce: many sets inside the window fire ONE save with the latest value", () => {
  const timers = manualTimers();
  const saved = [];
  const d = fb.makeDebounce(
    600,
    (v) => saved.push(v),
    timers.schedule,
    timers.cancel,
  );
  d.set("a");
  d.set("ab");
  d.set("abc");
  assert.deepEqual(saved, [], "nothing fires before the window elapses");
  assert.equal(timers.count(), 1, "exactly one timer pends");
  timers.fire();
  assert.deepEqual(saved, ["abc"]);
  assert.equal(d.pending(), false);
});

test("debounce: flush() forces the pending value out and clears pending", () => {
  const timers = manualTimers();
  const saved = [];
  const d = fb.makeDebounce(
    600,
    (v) => saved.push(v),
    timers.schedule,
    timers.cancel,
  );
  d.set("typed just before pagehide");
  d.flush();
  assert.deepEqual(saved, ["typed just before pagehide"]);
  assert.equal(
    timers.count(),
    0,
    "the timer was cancelled, not left to double-fire",
  );
  d.flush();
  assert.deepEqual(saved.length, 1, "flush with nothing pending is a no-op");
});

// ----- pinFromClient ------------------------------------------------------
test("pin math: center maps to 50/50, corners clamp to 0..100", () => {
  assert.deepEqual(fb.pinFromClient(640, 400, 1280, 800), {
    x_pct: 50,
    y_pct: 50,
  });
  assert.deepEqual(fb.pinFromClient(0, 0, 1280, 800), { x_pct: 0, y_pct: 0 });
  const corner = fb.pinFromClient(1280, 800, 1280, 800);
  assert.deepEqual(corner, { x_pct: 100, y_pct: 100 });
  const past = fb.pinFromClient(1300, 900, 1280, 800);
  assert.ok(past.x_pct <= 100 && past.y_pct <= 100, "never exceeds 100");
});

test("pin math: a zero viewport is refused, not divided by", () => {
  assert.throws(() => fb.pinFromClient(10, 10, 0, 800), /real viewport/);
});

test("pinStyle renders percent offsets", () => {
  assert.equal(fb.pinStyle({ x_pct: 12.5, y_pct: 80 }), "left:12.5%;top:80%");
});

// ----- pinBodyPos (issue #928) ---------------------------------------------
// The hover/reopened pin body hangs down-right of the marker; it must be
// nudged fully on-screen using the body's REAL measured size, not a guessed
// constant, by reusing clampPanelPos rather than a second clamp approach.
test("pinBodyPos clamps a bottom-right pin fully on-viewport using the real card size", () => {
  const pos = fb.pinBodyPos(
    { x_pct: 98.48, y_pct: 97 },
    { w: 240, h: 96 },
    { w: 1280, h: 800 },
  );
  assert.deepEqual(pos, { x: 1280 - 240 - 8, y: 800 - 96 - 8 });
});

test("pinBodyPos leaves a pin nowhere near an edge at its own point", () => {
  const pos = fb.pinBodyPos(
    { x_pct: 10, y_pct: 10 },
    { w: 240, h: 96 },
    { w: 1280, h: 800 },
  );
  assert.deepEqual(pos, { x: 128, y: 80 });
});

test("pinBodyPos delegates to clampPanelPos rather than a second clamp", () => {
  const pin = { x_pct: 95, y_pct: 95 };
  const panel = { w: 252, h: 320 };
  const viewport = { w: 1000, h: 700 };
  const expected = fb.clampPanelPos(
    { x: (pin.x_pct / 100) * viewport.w, y: (pin.y_pct / 100) * viewport.h },
    panel,
    viewport,
  );
  assert.deepEqual(fb.pinBodyPos(pin, panel, viewport), expected);
});

// ----- describeAnchor -----------------------------------------------------
function el({ id, testid, label, tag, classes, parent }) {
  return {
    id: id ?? "",
    tagName: tag ?? "DIV",
    getAttribute: (name) =>
      name === "data-testid"
        ? (testid ?? null)
        : name === "aria-label"
          ? (label ?? null)
          : null,
    classList: {
      length: classes?.length ?? 0,
      item: (i) => classes?.[i] ?? null,
    },
    parentElement: parent ?? null,
  };
}

test("anchor: an id wins immediately", () => {
  assert.equal(fb.describeAnchor(el({ id: "vibe-meter" })), "#vibe-meter");
});

test("anchor: walks up to an ancestor data-testid", () => {
  const target = el({ parent: el({ testid: "deck-a" }) });
  assert.equal(fb.describeAnchor(target), '[data-testid="deck-a"]');
});

test("anchor: aria-label carries its tag name", () => {
  const target = el({ label: "master volume", tag: "BUTTON" });
  assert.equal(fb.describeAnchor(target), 'button[aria-label="master volume"]');
});

test("anchor: a class is a last resort at the clicked element only", () => {
  assert.equal(fb.describeAnchor(el({ classes: ["clock"] })), ".clock");
  const deepAnonymous = el({ parent: el({ classes: ["layout-grid"] }) });
  assert.equal(
    fb.describeAnchor(deepAnonymous),
    null,
    "ancestor classes describe layout, not the thing clicked",
  );
});

test("anchor: a fully anonymous chain yields null, never a fabricated selector", () => {
  let chain = el({});
  for (let i = 0; i < 12; i += 1) chain = el({ parent: chain });
  assert.equal(fb.describeAnchor(chain), null);
  assert.equal(fb.describeAnchor(null), null);
});

// ----- startsPanelDrag ----------------------------------------------------
/**
 * Regression: a close-button click must dismiss the panel.
 *
 * The panel close X is a <button> nested INSIDE .fb-panel-head, and the head
 * is the drag handle. Its pointerdown handler called setPointerCapture on the
 * head, and pointer capture retargets the derived events to the capture
 * element: measured live in Chrome at 1280x800, pointerdown landed on
 * BUTTON.fb-mini but pointerup AND click both landed on DIV.fb-panel-head, so
 * the button's own onclick never ran and the panel never closed.
 *
 * The fix suppresses the drag when the pointer went down on a control that
 * owns its own click, which leaves that click intact.
 */
const node = (tagName, parentElement = null) => ({ tagName, parentElement });

test("drag: a pointerdown on the close X does not start a panel drag", () => {
  const head = node("DIV");
  assert.equal(
    fb.startsPanelDrag(node("BUTTON", head), head),
    false,
    "capturing the pointer here retargets the click and the X never closes the panel",
  );
});

test("drag: a pointerdown on an icon inside a header control keeps the click", () => {
  const head = node("DIV");
  const button = node("BUTTON", head);
  assert.equal(fb.startsPanelDrag(node("svg", button), head), false);
});

test("drag: a pointerdown on bare header chrome still starts a panel drag", () => {
  const head = node("DIV");
  assert.equal(fb.startsPanelDrag(head, head), true, "the panel must stay draggable");
  assert.equal(fb.startsPanelDrag(node("SPAN", head), head), true);
});

test("drag: the walk stops at the handle, so controls OUTSIDE it never suppress", () => {
  // The handle sits inside the topbar, which is full of buttons. Walking past
  // the handle would let an unrelated ancestor control kill the drag.
  const outerButton = node("BUTTON");
  const head = node("DIV", outerButton);
  assert.equal(fb.startsPanelDrag(head, head), true);
  assert.equal(fb.startsPanelDrag(node("SPAN", head), head), true);
});

test("drag: a detached target with no handle in its chain still drags", () => {
  assert.equal(fb.startsPanelDrag(null, null), true);
  assert.equal(fb.startsPanelDrag(node("DIV"), null), true);
});

// ----- the wiring, which no pure-logic test can see -----------------------
/**
 * startsPanelDrag is inert unless handleDragDown actually consults it BEFORE
 * setPointerCapture. Reverting only the component would leave every test
 * above green while the X stayed dead, so guard the wiring in the source the
 * same way FB-07 guards the cluster placement.
 */
const PANEL_SOURCE = readFileSync(
  fileURLToPath(new URL("../../src/lib/components/rb/FeedbackPanel.svelte", import.meta.url)),
  "utf8",
);

test("drag: handleDragDown consults startsPanelDrag before capturing the pointer", () => {
  const body = PANEL_SOURCE.slice(
    PANEL_SOURCE.indexOf("function handleDragDown"),
    PANEL_SOURCE.indexOf("function handleDragMove"),
  );
  assert.ok(body.length > 0, "if handleDragDown is not found then this guard asserts nothing");
  const guardAt = body.indexOf("startsPanelDrag");
  const captureAt = body.indexOf("setPointerCapture");
  assert.notEqual(guardAt, -1, "handleDragDown must ask whether this pointerdown may drag");
  assert.notEqual(captureAt, -1, "if the drag stops capturing the pointer then update this guard");
  assert.ok(
    guardAt < captureAt,
    "the guard must run BEFORE setPointerCapture, or the click is already retargeted",
  );
});

test("drag: the close X still carries the selectors the panel is driven by", () => {
  assert.match(PANEL_SOURCE, /aria-label="Close review panel"/);
  assert.match(PANEL_SOURCE, /class="fb-mini fb-close"/);
  assert.match(PANEL_SOURCE, /onclick=\{toggleFeedbackPanel\}/);
});

// ----- pin visibility preference -----------------------
// [if] a brand-new viewer with no stored preference sees pins already drawn
//   [then ⛔️] the "default OFF for a new viewer" requirement is broken
// [if] a viewer who already opted in loses that choice on reload
//   [then ⛔️] "an existing viewer with a stored preference keeps it" is broken
test("parsePinsVisible: a new viewer (no stored value) defaults OFF", () => {
  assert.equal(fb.parsePinsVisible(null), false);
});

test("parsePinsVisible: null is the only OFF-by-default value, an empty string is malformed", () => {
  assert.equal(fb.parsePinsVisible(null), false);
  assert.throws(() => fb.parsePinsVisible(""), /pinsVisible/);
});

test("parsePinsVisible: any stored value other than null/\"0\"/\"1\" is malformed, not a silent OFF", () => {
  assert.throws(() => fb.parsePinsVisible("garbage"), /pinsVisible/);
  assert.throws(() => fb.parsePinsVisible("true"), /pinsVisible/);
  assert.throws(() => fb.parsePinsVisible("2"), /pinsVisible/);
});

test("parsePinsVisible: an existing viewer's stored choice round-trips both ways", () => {
  assert.equal(fb.parsePinsVisible(fb.serializePinsVisible(true)), true);
  assert.equal(fb.parsePinsVisible(fb.serializePinsVisible(false)), false);
});

// ----- linkifying an agent's plain-text note -----------
// [if] linkifyAgentNote drops or mangles any of the original characters
//   [then ⛔️] an agent note renders with lost or corrupted text
// [if] a bare https URL is not recognised as a link segment
//   [then ⛔️] the reviewer cannot click through to what the agent referenced
test("linkifyAgentNote: plain text with no URL is one text segment", () => {
  const segments = fb.linkifyAgentNote("fixed in the next release");
  assert.deepEqual(segments, [{ type: "text", value: "fixed in the next release" }]);
});

test("linkifyAgentNote: a bare https URL becomes its own link segment", () => {
  const segments = fb.linkifyAgentNote("see https://github.com/x/y/issues/1 for detail");
  assert.deepEqual(segments, [
    { type: "text", value: "see " },
    { type: "link", value: "https://github.com/x/y/issues/1" },
    { type: "text", value: " for detail" },
  ]);
});

test("linkifyAgentNote: http (not just https) is also recognised", () => {
  const segments = fb.linkifyAgentNote("http://example.com");
  assert.deepEqual(segments, [{ type: "link", value: "http://example.com" }]);
});

test("linkifyAgentNote: two URLs in one note both become link segments", () => {
  const segments = fb.linkifyAgentNote("https://a.example one, https://b.example two");
  assert.deepEqual(segments, [
    { type: "link", value: "https://a.example" },
    { type: "text", value: " one, " },
    { type: "link", value: "https://b.example" },
    { type: "text", value: " two" },
  ]);
});

test("linkifyAgentNote: joining every segment's value reconstructs the original text exactly", () => {
  const original = "  weird   spacing https://x.example/a?b=1&c=2 . trailing text  ";
  const segments = fb.linkifyAgentNote(original);
  assert.equal(segments.map((s) => s.value).join(""), original);
});

test("linkifyAgentNote: an empty note is an empty segment list, not a crash", () => {
  assert.deepEqual(fb.linkifyAgentNote(""), []);
});

test("linkifyAgentNote: trailing sentence punctuation is not swallowed into the href", () => {
  const segments = fb.linkifyAgentNote("See https://example.com. Done.");
  assert.deepEqual(segments, [
    { type: "text", value: "See " },
    { type: "link", value: "https://example.com" },
    { type: "text", value: ". Done." },
  ]);
});

test("linkifyAgentNote: a URL in parentheses does not absorb the closing paren", () => {
  const segments = fb.linkifyAgentNote("(see https://example.com/x)");
  assert.deepEqual(segments, [
    { type: "text", value: "(see " },
    { type: "link", value: "https://example.com/x" },
    { type: "text", value: ")" },
  ]);
});

test("linkifyAgentNote: a URL followed by a comma keeps the comma out of the link", () => {
  const segments = fb.linkifyAgentNote("https://example.com, and more");
  assert.deepEqual(segments, [
    { type: "link", value: "https://example.com" },
    { type: "text", value: ", and more" },
  ]);
});

test("linkifyAgentNote: trailing punctuation trimming still reconstructs the original text exactly", () => {
  const original = "See https://example.com/a?b=1(x), (y). end.";
  const segments = fb.linkifyAgentNote(original);
  assert.equal(segments.map((s) => s.value).join(""), original);
});
