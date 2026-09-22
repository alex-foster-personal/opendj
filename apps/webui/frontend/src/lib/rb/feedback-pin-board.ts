/**
 * Comment pin board draw rules (issue #3782, FB-15).
 *
 * Pixel position (x_pct/y_pct) is canonical. Anchor and page are diagnostic;
 * mismatches surface badges instead of hiding or moving the pin.
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
