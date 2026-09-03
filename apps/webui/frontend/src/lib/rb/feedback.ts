/**
 * Pure logic for the in-app review/feedback widget (FB-01, FB-03).
 *
 * Everything here is dependency-free on purpose: the panel drag math, the
 * localStorage position codec, the debounced auto-save and the pin placement
 * math are the parts most worth unit-testing, so they live where node:test
 * can bundle them without a DOM or a Svelte runtime.
 *
 * localStorage carries ONLY the panel position and, since #858, which pin
 * updates this viewer has already read - both per-viewer conveniences.
 * Feedback CONTENT never touches client storage; it goes straight to
 * /api/v1/feedback so an agent harvest can never miss it.
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
 *   ✔︎ 🎯 pinStatus/isPinDrawn/pinIsDone: a pin with no status is open, an
 *     unknown status is open, only archived leaves the canvas, and only a
 *     fixed/merged pin offers Archive and Follow-on.
 *     [if] a pre-#858 pin (status null) stops being drawn [then ⛔️] broken
 *     [if] an open pin offers Archive [then ⛔️] broken
 *   ✔︎ 🎯 isPinUnread/markPinSeen/parsePinSeen: stamp-compared unread marker;
 *     junk in storage reads as an empty map.
 *     [if] a pin no agent touched wears a blue dot [then ⛔️] broken
 *     [if] a SECOND update after a read leaves no dot [then ⛔️] broken
 *   ✔︎ 🎯 describeAnchor: nearest stable identifier (id > data-testid >
 *     aria-label > class), null when nothing stable exists - never a guess.
 *     [if] an anonymous div chain yields a fabricated selector [then ⛔️] broken
 */

export interface PanelPos {
  x: number;
  y: number;
}

export const PANEL_POS_KEY = "odj-feedback-panel-pos";

/** Keep at least this many px of the panel header reachable on both axes. */
// ----- panel position ----------------------------------------------------
/** Keep the whole panel on-viewport: clamp to [0, viewport - panel] per axis. */
export function clampPanelPos(
  pos: PanelPos,
  panel: { w: number; h: number },
  viewport: { w: number; h: number },
): PanelPos {
  const maxX = Math.max(0, viewport.w - panel.w);
  const maxY = Math.max(0, viewport.h - panel.h);
  return {
    x: Math.min(Math.max(pos.x, 0), maxX),
    y: Math.min(Math.max(pos.y, 0), maxY),
  };
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

// ----- pin lifecycle (issue #858) ----------------------------------------
/**
 * The five states a comment pin moves through. A pin NEVER disappears on its
 * own: only `archived` leaves the canvas, and only because the maintainer pressed
 * Archive on a pin whose work is done.
 */
export type PinStatus = "open" | "issued" | "fixed" | "merged" | "archived";

const PIN_STATUSES: readonly string[] = [
  "open",
  "issued",
  "fixed",
  "merged",
  "archived",
];

/** The one localStorage key this feature owns: {pin id: updated_at seen}. */
export const PIN_SEEN_KEY = "mdt.feedback.pinSeen.v1";

/** What the lifecycle reads off a pin. A subset of CommentOut so node:test
 * can drive these with plain objects and no generated client types. */
export interface LifecyclePin {
  id: string;
  status?: string | null;
  issue_url?: string | null;
  updated_at?: string | null;
}

/**
 * A pin's status, defaulted and validated.
 *
 * Pins created before Wed 2 Sep 2026 carry no status at all (22 of the 35 on
 * this machine), and an unrecognised string is a typo'd PATCH rather than a
 * new state. Both read as `open`: the amber default is the honest answer, and
 * it is the state that hides nothing.
 */
export function pinStatus(pin: LifecyclePin): PinStatus {
  const raw = pin.status ?? "open";
  return (PIN_STATUSES.includes(raw) ? raw : "open") as PinStatus;
}

/** Archived pins leave the canvas; everything else stays on it forever. */
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

// ----- nearest stable anchor ---------------------------------------------
/** The slice of Element the anchor walk reads; tests pass plain objects. */
export interface AnchorishElement {
  id?: string;
  tagName?: string;
  getAttribute?(name: string): string | null;
  classList?: { length: number; item(i: number): string | null };
  parentElement?: AnchorishElement | null;
}

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
