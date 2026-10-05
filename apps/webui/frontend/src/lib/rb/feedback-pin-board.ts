/**
 * Comment pin board draw rules (issue #3782, FB-15).
 *
 * Pixel position (x_pct/y_pct) is canonical. The anchor is diagnostic: a
 * mismatch surfaces a badge instead of hiding or moving the pin. The page is
 * a draw rule (pin 4860c440): a pin is drawn only on the route it was placed
 * on, see isPinOnPage.
 */

import { describeAnchor, pinStatus, type AnchorishElement, type LifecyclePin } from "./feedback";

export type PinBoardBadge = "anchor-moved" | "route-moved" | "harvested" | "regressed";

export interface PinBoardState {
  drawn: boolean;
  collapsed: boolean;
  badges: PinBoardBadge[];
}

/** Only explicit per-pin archive removes a pin from the canvas. */
export function isPinDrawn(pin: LifecyclePin): boolean {
  return pinStatus(pin) !== "archived";
}

export function isPinRegressed(pin: LifecyclePin): boolean {
  const fixedAt = pin.fixed_at;
  const fixedSha = pin.fixed_in_sha;
  const updated = pin.updated_at;
  if (!fixedSha || !fixedAt || !updated) return false;
  return updated > fixedAt;
}

export function isRouteMoved(pin: LifecyclePin, pathname: string): boolean {
  return pin.page !== pathname;
}

function _routeKey(pathname: string): string {
  // The router ignores trailing slashes (routes/+layout.ts), so `/x` and
  // `/x/` are one page.
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

/** True when the pin belongs on the current route (pin 4860c440). A pin's
 * x_pct/y_pct only mean something against the page it was placed on, so the
 * global layer draws it there and nowhere else. A pin with no recorded page
 * cannot be attributed to any route; it is drawn everywhere, because hiding
 * it everywhere would lose the feedback silently. */
export function isPinOnPage(pin: LifecyclePin, pathname: string): boolean {
  if (pin.page === undefined || pin.page === "") return true;
  return _routeKey(pin.page) === _routeKey(pathname);
}

/** Browser-only: re-resolve anchor at stored pixel position. */
export function anchorMovedAtPin(
  pin: LifecyclePin,
  elementFromPoint: (x: number, y: number) => AnchorishElement | null
): boolean {
  if (!pin.anchor) return false;
  // Pixel position is the durable reference; a pin without one has nothing
  // to re-resolve against, so it cannot be reported as moved.
  if (pin.x_pct === undefined || pin.y_pct === undefined) return false;
  const vw = pin.environment?.viewport_width ?? window.innerWidth;
  const vh = pin.environment?.viewport_height ?? window.innerHeight;
  const x = (pin.x_pct / 100) * vw;
  const y = (pin.y_pct / 100) * vh;
  const el = elementFromPoint(x, y);
  const current = describeAnchor(el);
  return current !== pin.anchor;
}

export function pinBoardState(
  pin: LifecyclePin,
  pathname: string,
  anchorMoved: boolean
): PinBoardState {
  const status = pinStatus(pin);
  const badges: PinBoardBadge[] = [];
  if (status === "harvested") badges.push("harvested");
  if (isPinRegressed(pin)) badges.push("regressed");
  if (isRouteMoved(pin, pathname)) badges.push("route-moved");
  if (anchorMoved && pin.anchor) badges.push("anchor-moved");
  return {
    drawn: isPinDrawn(pin),
    collapsed: status === "harvested",
    badges,
  };
}

export const SHOW_HARVESTED_PINS_KEY = "mdt.feedback.showHarvestedPins.v1";

export function parseShowHarvestedPins(raw: string | null): boolean {
  if (raw === null) return true;
  if (raw === "0") return false;
  if (raw === "1") return true;
  throw new Error(`showHarvestedPins: unrecognised stored value ${JSON.stringify(raw)}`);
}

export function serializeShowHarvestedPins(visible: boolean): string {
  return visible ? "1" : "0";
}
