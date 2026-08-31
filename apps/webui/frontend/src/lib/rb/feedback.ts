/**
 * Pure logic for the in-app review/feedback widget (FB-01, FB-03).
 *
 * Everything here is dependency-free on purpose: the panel drag math, the
 * localStorage position codec, the debounced auto-save and the pin placement
 * math are the parts most worth unit-testing, so they live where node:test
 * can bundle them without a DOM or a Svelte runtime.
 *
 * localStorage carries ONLY the panel position - a per-viewer convenience.
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
