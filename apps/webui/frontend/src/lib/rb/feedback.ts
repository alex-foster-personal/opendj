/**
 * Pure logic for the in-app review/feedback widget (FB-01, FB-03).
 *
 * Everything here is dependency-free on purpose: the panel drag math, the
 * localStorage position codec, the debounced auto-save and the pin placement
 * math are the parts most worth unit-testing, so they live where node:test
 * can bundle them without a DOM or a Svelte runtime.
 *
 * localStorage carries the panel position, ONE unsent pin draft, and since
 * #858 which pin updates this viewer has already read - all per-viewer
 * conveniences. Feedback CONTENT still never touches client storage: the
 * moment a pin is saved it goes to /api/v1/feedback and the draft is dropped,
 * so an agent harvest can never miss anything the store has. A draft is the
 * other thing - text that has never been submitted, so there is nothing for a
 * harvest to miss, and losing it on a refresh was pin 307e0e84bfbe. Parking it
 * locally rather than POSTing it half-written is deliberate: autosaving
 * drafts to the daemon would fill the maintainer's own review list with fragments.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 clampPanelPos: a dragged position is kept fully on-viewport.
 *     [if] a panel dragged past the right edge restores off-screen [then ⛔️] broken
 *   ✔︎ 🎯 parsePanelPos: only a finite {x, y} round-trips; garbage reads null.
 *     [if] localStorage junk crashes the panel mount [then ⛔️] broken
 *   ✔︎ 🎯 startsPanelDrag: a pointerdown on a control inside the drag handle
 *     suppresses the drag, so pointer capture cannot swallow that click.
 *     [if] the close X inside the header starts a drag [then ⛔️] broken
 *     [if] bare header chrome stops dragging the panel [then ⛔️] broken
 *   ✔︎ 🎯 makeDebounce: trailing-edge save; flush() forces the pending write.
 *     [if] two keystrokes inside the window issue two saves [then ⛔️] broken
 *     [if] flush() drops a pending value [then ⛔️] broken
 *   ✔︎ 🎯 pinFromClient: a viewport click becomes 0..100 percents, clamped.
 *     [if] a click at the exact bottom-right corner exceeds 100 [then ⛔️] broken
 *   ✔︎ 🎯 pinBodyPos: a reopened/hovered pin body's MEASURED size is clamped
 *     fully on-viewport (issue #928), reusing clampPanelPos.
 *     [if] a pin near the bottom or a side opens with its body off-screen
 *       [then ⛔️] broken
 *   ✔︎ 🎯 pinStatus/isPinDrawn/pinIsDone: a pin with no status is open, an
 *     unknown status is open, only archived leaves the canvas, and only a
 *     fixed/merged pin offers Archive and Follow-on.
 *     [if] a pre-#858 pin (status null) stops being drawn [then ⛔️] broken
 *     [if] an open pin offers Archive [then ⛔️] broken
 *   ✔︎ 🎯 isPartialNote/pinVisualState (feedback-pin-partial.ts, split out to
 *     stay under the file-size gate): a HALF-FIXED pin (pin 58a16ac781db,
 *     follow-on to #907) is a reply convention, not a new persisted status.
 *     See that file's own docstring for the acceptance tests.
 *   ✔︎ 🎯 isPinUnread/markPinSeen/parsePinSeen: stamp-compared unread marker;
 *     junk in storage reads as an empty map.
 *     [if] a pin no agent touched wears a blue dot [then ⛔️] broken
 *     [if] a SECOND update after a read leaves no dot [then ⛔️] broken
 *   ✔︎ 🎯 describeAnchor: nearest stable identifier (id > data-testid >
 *     aria-label > class), null when nothing stable exists - never a guess.
 *     [if] an anonymous div chain yields a fabricated selector [then ⛔️] broken
 *   ✔︎ 🎯 parsePinsVisible/serializePinsVisible: a new viewer (no stored
 *     value) defaults pin markers OFF; an existing viewer's choice round-trips;
 *     any other stored value is rejected, never silently read as OFF.
 *     [if] a brand-new viewer sees pins already drawn [then ⛔️] broken
 *     [if] an opted-in viewer loses that choice on reload [then ⛔️] broken
 *     [if] a corrupted stored value is read as a silent OFF [then ⛔️] broken
 *   ✔︎ 🎯 linkifyAgentNote: splits plain text around bare http/https URLs
 *     without dropping or mangling any character; never markup.
 *     [if] a bare URL in an agent note is not its own link segment [then ⛔️] broken
 *     [if] joining every segment's value does not reconstruct the original
 *       text exactly [then ⛔️] broken
 */

import { clampToViewport } from "$lib/ui/clamp-to-viewport";
import type { AnchorishElement } from "./feedback-anchorish";
import type { PinPlacement } from "./feedback-pin-position";

export interface PanelPos {
  x: number;
  y: number;
}

export const PANEL_POS_KEY = "odj-feedback-panel-pos";

/** Where an UNSENT pin draft is parked so a refresh cannot eat it. */
export const PIN_DRAFT_KEY = "odj-feedback-pin-draft";

/** Keep at least this many px of the panel header reachable on both axes. */
// ----- panel position ----------------------------------------------------
/** Keep the whole panel on-viewport with the shared 8 px margin. */
export function clampPanelPos(
  pos: PanelPos,
  panel: { w: number; h: number },
  viewport: { w: number; h: number },
): PanelPos {
  const box = clampToViewport(
    pos.x,
    pos.y,
    { width: panel.w, height: panel.h },
    { width: viewport.w, height: viewport.h },
  );
  return { x: box.x, y: box.y };
}

export function parsePanelPos(raw: string | null): PanelPos | null {
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const { x, y } = parsed as Record<string, unknown>;
  if (typeof x !== "number" || typeof y !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

export function serializePanelPos(pos: PanelPos): string {
  return JSON.stringify({ x: Math.round(pos.x), y: Math.round(pos.y) });
}

// ----- panel drag suppression --------------------------------------------
/** Tags inside the drag handle that own their own click and must keep it. */
const DRAG_BLOCKING_TAGS = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA"]);

/** Defensive bound on the ancestor walk. The handle is a handful of nodes up,
 * so this only exists so a malformed chain cannot spin forever. */
const DRAG_WALK_LIMIT = 16;

/** The shape startsPanelDrag needs off a pointer event: DOM-free so node:test
 * can drive it with plain objects. */
export interface DragNode {
  tagName?: string;
  parentElement?: DragNode | null;
}

/**
 * True when a pointerdown on `target` may begin dragging the panel by `handle`.
 *
 * The close X sits INSIDE the drag handle, so its pointerdown bubbles to the
 * header. Beginning a drag there calls setPointerCapture on the header, and
 * pointer capture retargets the derived pointerup and click to the capture
 * element - so the button's own onclick never fires and the X looks dead.
 * Measured in Chrome, Mon 31 Aug 2026: pointerdown on BUTTON.fb-mini, then
 * pointerup and click both on DIV.fb-panel-head.
 *
 * Controls between `target` and `handle` therefore suppress the drag and keep
 * their click. The walk STOPS at the handle: the handle itself sits inside a
 * topbar full of buttons, and walking past it would let an unrelated ancestor
 * control kill dragging entirely.
 */
export function startsPanelDrag(
  target: DragNode | null,
  handle: DragNode | null,
): boolean {
  let node: DragNode | null | undefined = target;
  for (let hops = 0; node && hops < DRAG_WALK_LIMIT; hops += 1) {
    if (node === handle) return true;
    if (DRAG_BLOCKING_TAGS.has((node.tagName ?? "").toUpperCase())) return false;
    node = node.parentElement;
  }
  return true;
}

// ----- debounced auto-save ------------------------------------------------
export interface Debounced<T> {
  /** Record the latest value; (re)starts the trailing timer. */
  set(value: T): void;
  /** Fire the pending save NOW (blur, unmount). No-op when nothing pends. */
  flush(): void;
  /** True while a save is scheduled but not yet fired. */
  pending(): boolean;
}

type Schedule = (fn: () => void, ms: number) => unknown;
type Cancel = (handle: unknown) => void;

/**
 * Trailing-edge debounce for auto-save. Timer functions are injectable so
 * tests drive time by hand instead of sleeping.
 */
export function makeDebounce<T>(
  delayMs: number,
  run: (value: T) => void,
  schedule: Schedule = (fn, ms) => setTimeout(fn, ms),
  cancel: Cancel = (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
): Debounced<T> {
  let handle: unknown = null;
  let latest: T;
  const fire = (): void => {
    handle = null;
    run(latest);
  };
  return {
    set(value: T): void {
      latest = value;
      if (handle !== null) cancel(handle);
      handle = schedule(fire, delayMs);
    },
    flush(): void {
      if (handle === null) return;
      cancel(handle);
      fire();
    },
    pending(): boolean {
      return handle !== null;
    },
  };
}

// ----- pin placement math -------------------------------------------------
export interface PinPoint {
  x_pct: number;
  y_pct: number;
}

export function pinFromClient(
  clientX: number,
  clientY: number,
  viewportW: number,
  viewportH: number,
): PinPoint {
  if (viewportW <= 0 || viewportH <= 0) {
    throw new Error(
      `pinFromClient needs a real viewport, got ${viewportW}x${viewportH}`,
    );
  }
  const pct = (value: number, span: number): number =>
    Math.min(100, Math.max(0, Math.round((value / span) * 10000) / 100));
  return { x_pct: pct(clientX, viewportW), y_pct: pct(clientY, viewportH) };
}

export function pinStyle(pin: PinPoint): string {
  return `left:${pin.x_pct}%;top:${pin.y_pct}%`;
}

/**
 * Where a pin's BODY sits. The marker itself is centred on its point, but the
 * body hangs down and right from it, so a pin near an edge would open its
 * Archive and Follow-on buttons off-screen - and one of the pins on this
 * machine sits at 98.48%. min() holds it inside the viewport in CSS, which
 * costs no measuring pass and no resize listener.
 *
 * The vertical clamp uses `.fb-pin-body`'s CSS `max-height: 320px`, not a
 * guess: `box-sizing: border-box` (app.css) means the padding is already
 * INSIDE that 320px, so it is also the actual rendered height cap. A long
 * agent note can grow the body all the way to it, and clamping to less than
 * the actual maximum still lets Archive/Follow-on/Close render below the
 * fold.
 *
 * Each calc() is floored at `max(0px, ...)`: below a 252px-wide or
 * 320px-tall viewport the calc alone goes negative and min() would place
 * the body off-screen instead of just against the edge.
 */
export function pinBodyStyle(pin: PinPoint): string {
  return (
    `left:min(${pin.x_pct}%, max(0px, calc(100vw - 252px)));` +
    `top:min(${pin.y_pct}%, max(0px, calc(100vh - 320px)))`
  );
}

/**
 * Where a pin's body sits, from its ACTUAL rendered size rather than the
 * fixed 252x320 guess `pinBodyStyle` uses. The guess is safe (it always
 * over-reserves) but wrong: `.fb-pin-body` renders at 240px wide with the
 * repo-wide `box-sizing: border-box`, and its true height is whatever the
 * pin's own text and agent note measure, almost always well under the
 * 320px max-height cap. Reserving the cap every time nudges a short pin's
 * body further from its marker than the body actually needs.
 *
 * `clampPanelPos` (issue #928 review) already solves exactly this problem
 * for the review panel, so this composes it rather than inventing a second
 * clamp: the pin's percent point becomes a px position against the live
 * viewport, then that position is clamped fully on-screen by the caller's
 * MEASURED panel size (`FeedbackPinCard`'s own `getBoundingClientRect()`).
 */
export function pinBodyPos(
  pin: PinPoint,
  panel: { w: number; h: number },
  viewport: { w: number; h: number },
): PanelPos {
  const pos = {
    x: (pin.x_pct / 100) * viewport.w,
    y: (pin.y_pct / 100) * viewport.h,
  };
  return clampPanelPos(pos, panel, viewport);
}

// ----- pin lifecycle (issue #858) ----------------------------------------
/**
 * The six states a comment pin moves through. `blocked` is deliberately narrow:
 * credentials/auth, a destructive-action decision, or a genuine product fork
 * that needs the maintainer. Unclear instructions are a question in the agent note, never
 * blocked. A blocked note begins with one sentence naming what the maintainer must provide.
 * A pin NEVER disappears on its own: only `archived` leaves the canvas, and only
 * because the maintainer pressed Archive on a pin whose work is done. Pixel position
 * (x_pct/y_pct) is the durable reference; anchor labels are diagnostic only.
 */
const PIN_STATUSES = ["open", "issued", "blocked", "fixed", "merged", "harvested", "archived"] as const;
export type PinStatus = (typeof PIN_STATUSES)[number];

/** The one localStorage key this feature owns: {pin id: updated_at seen}. */
export const PIN_SEEN_KEY = "mdt.feedback.pinSeen.v1";

/** What the lifecycle reads off a pin. A subset of CommentOut so node:test
 * can drive these with plain objects and no generated client types. */
export interface LifecyclePin {
  id: string;
  x_pct?: number;
  y_pct?: number;
  anchor?: string | null;
  page?: string;
  status?: string | null;
  issue_url?: string | null;
  updated_at?: string | null;
  agent_note?: string | null;
  fixed_in_sha?: string | null;
  fixed_at?: string | null;
  environment?: { viewport_width?: number; viewport_height?: number } | null;
}

/**
 * A pin's status, defaulted and validated.
 *
 * Pins created before Wed 2 Sep 2026 carry no status at all (22 of the 35 on
 * this machine), and an unrecognised string is a typo'd PATCH rather than a
 * new state. Both read as `open`: the amber default is the honest answer, and
 * it is the state that hides nothing.
 */
function _isPinStatus(raw: string): raw is PinStatus {
  return (PIN_STATUSES as readonly string[]).includes(raw);
}

export function pinStatus(pin: LifecyclePin): PinStatus {
  const raw = pin.status ?? "open";
  return _isPinStatus(raw) ? raw : "open";
}

// Partial state (pin 58a16ac781db, follow-on to #907) lives in
// feedback-pin-partial.ts, split out to keep this file under the 600-line
// file-size gate (Amendment 17: extraction, never a raised budget). It
// imports pinStatus/LifecyclePin/PinStatus from here; nothing here imports
// it back.

export interface PinStatusSummary {
	total: number;
	untriaged: number;
	open: number;
	issued: number;
	blocked: number;
	fixed: number;
	merged: number;
	harvested: number;
}

/** Count active statuses from the live comment board. Archived pins are moved
 * out of this board, and engine job states have no stable comment-id join, so
 * neither belongs in the comment-icon total. A missing status is deliberately
 * separate from explicit `open`: older pins have no triage record yet, while
 * an open pin was explicitly recorded as open. */
export function summarizePinStatuses(pins: readonly LifecyclePin[]): PinStatusSummary {
	const summary: PinStatusSummary = {
		total: 0,
		untriaged: 0,
		open: 0,
		issued: 0,
		blocked: 0,
		fixed: 0,
		merged: 0,
		harvested: 0
	};
	for (const pin of pins) {
		const status = pinStatus(pin);
		if (status === 'archived') continue;
		summary.total += 1;
    if (pin.status === null || pin.status === undefined) {
      summary.untriaged += 1;
    } else {
      summary[status] += 1;
    }
  }
  return summary;
}

export function describePinStatusSummary(pins: readonly LifecyclePin[]): string {
	const summary = summarizePinStatuses(pins);
	return `Active comment pins: ${summary.total} total - ` +
		`${summary.untriaged} untriaged, ${summary.open} open, ${summary.issued} issued, ${summary.blocked} blocked, ` +
		`${summary.fixed} fixed, ${summary.merged} merged, ${summary.harvested} harvested`;
}

export { blockedPinDetail, blockedPinRequest } from './feedback-blocked-note';

/** Archived pins leave the canvas; harvested and everything else stay on it. */
export function isPinDrawn(pin: LifecyclePin): boolean {
  return pinStatus(pin) !== "archived";
}

/** True once the work behind a pin is done, which is what unlocks Archive and
 * Follow-on. Offering them earlier invites archiving work still in flight. */
export function pinIsDone(pin: LifecyclePin): boolean {
  const status = pinStatus(pin);
  return status === "fixed" || status === "merged";
}

/** The prefix a follow-on pin opens with, naming the parent it came from. */
export function followOnText(pin: LifecyclePin): string {
  return `Follow-on to ${pin.issue_url ?? pin.id}: `;
}

// ----- the unread marker (the maintainer, Wed 2 Sep 2026 17:12) --------------------
/** Per-viewer, per-pin: the `updated_at` this viewer has already read. */
export type PinSeen = Record<string, string>;

/**
 * True when an agent has touched this pin since this viewer last opened it.
 *
 * Stamps are compared, not a read flag: a second agent update after a read
 * must raise the dot again, which a boolean cannot express. The stamps are
 * fixed-width UTC with microseconds (`%Y-%m-%dT%H:%M:%S.%fZ`), so string
 * order IS time order and no Date parsing is needed - and two writes inside
 * the same second still compare unequal.
 */
export function isPinUnread(pin: LifecyclePin, seen: PinSeen): boolean {
  const updated = pin.updated_at;
  if (!updated) return false; // no agent has ever written to it
  const at = seen[pin.id];
  return at === undefined || at < updated;
}

/** Record that this viewer has read the pin AS IT STANDS NOW. */
export function markPinSeen(seen: PinSeen, pin: LifecyclePin): PinSeen {
  if (!pin.updated_at) return seen;
  return { ...seen, [pin.id]: pin.updated_at };
}

export function parsePinSeen(raw: string | null): PinSeen {
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
    return {};
  const seen: PinSeen = {};
  for (const [id, at] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof at === "string") seen[id] = at;
  }
  return seen;
}

export function serializePinSeen(seen: PinSeen): string {
  return JSON.stringify(seen);
}

// ----- pin visibility preference (pin 88e3abec02a0) ----------------------
/** Per-viewer, localStorage-only: whether comment pin markers are drawn on
 * the canvas at all. A NEW viewer (no stored value) defaults OFF - the
 * feature is opt-in until this ships far enough that showing pins by default
 * is itself decided. An existing viewer's own choice always round-trips.
 * Fail-fast: only null (no stored value yet), "0" and "1" are valid: every
 * other value is corrupted or future-version state, and hiding that behind
 * a silent OFF would mask exactly the failure this file exists to surface. */
export const PINS_VISIBLE_KEY = "mdt.feedback.pinsVisible.v1";

export function parsePinsVisible(raw: string | null): boolean {
  if (raw === null) return false;
  if (raw === "0") return false;
  if (raw === "1") return true;
  throw new Error(`pinsVisible: unrecognised stored value ${JSON.stringify(raw)}`);
}

export function serializePinsVisible(visible: boolean): string {
  return visible ? "1" : "0";
}

// ----- linkifying an agent's plain-text note (pin 27fe1e3e61b5) ----------
// Moved to feedback-note.ts (Thu 3 Sep 2026, pin review v2) to bring this
// file back under the 600-line file-size gate; re-exported here so nothing
// importing it from feedback.ts needs to change.
export { linkifyAgentNote } from "./feedback-note";

// ----- nearest stable anchor ---------------------------------------------
// AnchorishElement lives in feedback-anchorish.ts so feedback-pin-placement
// can name it without importing this module (that edge closed an import cycle).
export type { AnchorishElement } from "./feedback-anchorish";

const ANCHOR_WALK_LIMIT = 8;

/**
 * Best-effort nearest stable identifier for a dropped pin: walk up from the
 * clicked element looking for an id, a data-testid, or an aria-label. A bare
 * class name is a last resort at the clicked element only (ancestor classes
 * describe layout, not the thing clicked). Null when nothing stable exists.
 */
export function describeAnchor(start: AnchorishElement | null): string | null {
  let el: AnchorishElement | null | undefined = start;
  let hops = 0;
  while (el && hops < ANCHOR_WALK_LIMIT) {
    if (el.id) return `#${el.id}`;
    const testId = el.getAttribute?.("data-testid");
    if (testId) return `[data-testid="${testId}"]`;
    const label = el.getAttribute?.("aria-label");
    if (label) {
      const tag = (el.tagName ?? "element").toLowerCase();
      return `${tag}[aria-label="${label}"]`;
    }
    if (hops === 0) {
      const cls =
        el.classList && el.classList.length > 0 ? el.classList.item(0) : null;
      if (cls) return `.${cls}`;
    }
    el = el.parentElement;
    hops += 1;
  }
  return null;
}


// ----- unsent pin draft ---------------------------------------------------
export interface PinDraft {
  point: PinPoint;
  anchor: string | null;
  text: string;
  /** The pathname the draft's point/anchor were computed against, captured
   * at creation time (r3919185341). A draft is only ever meaningful on the
   * page it was placed on - restoring it under a different pathname and
   * saving with the CURRENT page would silently attach the comment to the
   * wrong page and reinterpret stale coordinates against a different UI. */
  page: string;
  /** Viewport captured at creation time. Optional only because an
   * older-shaped stored draft (pre this field) must still parse. */
  viewport?: { width: number; height: number };
  /** Follow-on parent link, if this draft is a reply-in-progress. Persisted
   * across a refresh (pin b0f-followon, FeedbackWidget.svelte:138 review):
   * dropping it on restore silently downgraded a follow-on draft to a
   * plain top-level draft, which is the bug this field exists to close. */
  followOn?: { parentId: string; label: string } | null;
  /** Where the click sits relative to the UI (feedback-pin-position.ts). */
  placement?: PinPlacement | null;
}

export function serializePinDraft(draft: PinDraft): string {
  return JSON.stringify(draft);
}


/**
 * Restore a parked draft, or null. Strict on purpose, the same way
 * parsePanelPos is: a half-shaped record must read as "no draft" rather than
 * restore a bubble with an undefined position on it. Whitespace-only text is
 * nothing to restore either - it would reopen an empty bubble over the page
 * on every load.
 */
export function parsePinDraft(raw: string | null): PinDraft | null {
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const d = parsed as Record<string, unknown>;
  const point = d.point as Record<string, unknown> | undefined;
  if (
    typeof point !== "object" ||
    point === null ||
    !Number.isFinite(point.x_pct as number) ||
    !Number.isFinite(point.y_pct as number)
  ) {
    return null;
  }
  if (typeof d.text !== "string" || d.text.trim() === "") return null;
  if (d.anchor !== null && typeof d.anchor !== "string") return null;
  if (typeof d.page !== "string" || d.page === "") return null;
  // followOn is optional and, when present, must be a well-formed
  // { parentId, label } record - a half-shaped one is worse than none,
  // since saving against a bad parentId would fail server-side.
  let followOn: { parentId: string; label: string } | null | undefined;
  if (d.followOn === null || d.followOn === undefined) {
    followOn = d.followOn === null ? null : undefined;
  } else if (
    typeof d.followOn === "object" &&
    typeof (d.followOn as Record<string, unknown>).parentId === "string" &&
    typeof (d.followOn as Record<string, unknown>).label === "string"
  ) {
    const fo = d.followOn as Record<string, unknown>;
    followOn = { parentId: fo.parentId as string, label: fo.label as string };
  } else {
    return null; // corrupt follow-on link - do not restore a broken draft
  }
  const vp = d.viewport as Record<string, unknown> | undefined;
  const viewport =
    typeof vp === "object" &&
    vp !== null &&
    Number.isFinite(vp.width as number) &&
    Number.isFinite(vp.height as number)
      ? { width: vp.width as number, height: vp.height as number }
      : undefined;
  // exactOptionalPropertyTypes forbids `viewport: undefined` / `followOn:
  // undefined` against PinDraft's optional (not `| undefined`) properties -
  // the key must be ABSENT, not present-with-undefined, when there is no
  // value to restore.
  return {
    point: { x_pct: point.x_pct as number, y_pct: point.y_pct as number },
    page: d.page,
    anchor: d.anchor,
    text: d.text,
    ...(viewport !== undefined ? { viewport } : {}),
    ...(followOn !== undefined ? { followOn } : {}),
  };
}
