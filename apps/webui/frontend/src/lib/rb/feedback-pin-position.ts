/**
 * Where a comment pin is drawn once the UI under it has changed (Thu 1 Oct
 * 2026). the maintainer keeps pins on the canvas as a record of what he asked for, and
 * seeing them sit ON the UI they talk about is what lets him organise them.
 * A viewport percent alone drifts off its element the moment the layout
 * moves, so a pin now also records where it sits relative to the UI, and is
 * re-placed against it on every render.
 *
 * Captured at placement time (the click), sent with the save:
 * - `element_offset`: the click inside the `anchor` element's own box, as
 *   0..100 of its width and height. Recorded only when the anchor selector
 *   re-finds exactly that element and the click is inside its box; a guess
 *   that resolves to some other element would be worse than no record.
 * - `nearby_anchors`: up to three other stable, page-unique elements near the
 *   click (id, data-testid, tag + aria-label), each with the pin's px offset
 *   from its top-left corner, closest offset first. Pixels, not percent,
 *   because a neighbour is often a sibling the point lies outside of.
 *
 * Resolved on render, in this order, first hit wins:
 *   1. `element`  - `anchor` + `element_offset`
 *   2. `anchor`   - the first nearby anchor that still resolves
 *   3. `viewport` - stored `x_pct`/`y_pct`, exactly as pins always drew
 * A selector "resolves" only when it matches exactly ONE element with a
 * non-empty box: two matches is ambiguous and a 0x0 box is hidden, and both
 * fall through to the next tier rather than guessing.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 resolvePinPosition: element, then nearby anchor, then viewport.
 *     [if] a pin whose element still resolves is drawn anywhere but on that
 *       element [then ⛔️] broken
 *     [if] a pin whose element is gone is drawn at its old viewport percent
 *       while a nearby anchor still resolves [then ⛔️] broken
 *     [if] a pin saved before capture existed is drawn anywhere but its
 *       x_pct/y_pct, or marked as on a fallback [then ⛔️] broken
 *   ✔︎ 🎯 buildPinPlacement: offset only for a click inside the primary box;
 *     at most three unique neighbours, closest first, never the primary.
 *     [if] a click outside the primary's box records an element offset
 *       [then ⛔️] broken
 *     [if] a fourth neighbour, or the primary itself, is stored as a nearby
 *       anchor [then ⛔️] broken
 *   ✔︎ 🎯 parsePinPlacement: a parked draft's placement round-trips; a
 *     half-shaped one reads as null rather than a broken record.
 *     [if] a refresh mid-draft drops a well-formed placement [then ⛔️] broken
 */

import { parseAnchorSelector } from './feedback-pin-anchor-highlight';
import { isPinSystemElement } from './feedback-pin-placement';

export const MAX_NEARBY_ANCHORS = 3;

/** How many ancestors, and siblings either side of each, the capture walk
 * considers. Bounded so a click deep in a long list stays cheap. */
const NEARBY_WALK_ANCESTORS = 10;
const NEARBY_WALK_SIBLINGS = 4;

export interface PinRect {
	left: number;
	top: number;
	width: number;
	height: number;
}

export interface PinElementOffset {
	dx_pct: number;
	dy_pct: number;
}

export interface PinNearbyAnchor {
	selector: string;
	dx_px: number;
	dy_px: number;
}

export interface PinPlacement {
	element_offset: PinElementOffset | null;
	nearby_anchors: PinNearbyAnchor[];
}

export type PinPositionTier = 'element' | 'anchor' | 'viewport';

/** The slice of a pin the resolver reads; a subset of CommentOut so tests
 * drive it with plain objects. */
export interface PositionedPin {
	x_pct: number;
	y_pct: number;
	anchor?: string | null;
	element_offset?: PinElementOffset | null;
	nearby_anchors?: readonly PinNearbyAnchor[] | null;
}

export interface ResolvedPinPosition {
	tier: PinPositionTier;
	/** True when the pin resolved on a lower tier than it was captured with,
	 * i.e. what it was pinned to has moved or gone. Never true for a pin
	 * saved before capture existed: its best tier IS the viewport. */
	fallback: boolean;
	/** The selector the position was resolved against; null on `viewport`. */
	via: string | null;
	/** Viewport px, for `position: fixed`. */
	x: number;
	y: number;
	/** The same point as viewport percent (the pin card's clamp reads these). */
	x_pct: number;
	y_pct: number;
}

const TIER_RANK: Record<PinPositionTier, number> = { element: 0, anchor: 1, viewport: 2 };

function _isMeasurable(rect: PinRect | null): rect is PinRect {
	return (
		rect !== null &&
		Number.isFinite(rect.left) &&
		Number.isFinite(rect.top) &&
		rect.width > 0 &&
		rect.height > 0
	);
}

function _bestCapturedTier(pin: PositionedPin): PinPositionTier {
	if (pin.anchor && pin.element_offset) return 'element';
	if ((pin.nearby_anchors?.length ?? 0) > 0) return 'anchor';
	return 'viewport';
}

function _resolved(
	tier: PinPositionTier,
	pin: PositionedPin,
	via: string | null,
	x: number,
	y: number,
	viewport: { w: number; h: number }
): ResolvedPinPosition {
	const pct = (value: number, span: number): number => Math.round((value / span) * 10000) / 100;
	return {
		tier,
		fallback: TIER_RANK[tier] > TIER_RANK[_bestCapturedTier(pin)],
		via,
		x,
		y,
		x_pct: pct(x, viewport.w),
		y_pct: pct(y, viewport.h)
	};
}

/**
 * Where to draw `pin` now. `measure` returns the live box of the one element
 * a selector names, or null when it names none, several, or a hidden one.
 */
export function resolvePinPosition(
	pin: PositionedPin,
	measure: (selector: string) => PinRect | null,
	viewport: { w: number; h: number }
): ResolvedPinPosition {
	if (viewport.w <= 0 || viewport.h <= 0) {
		throw new Error(`resolvePinPosition needs a real viewport, got ${viewport.w}x${viewport.h}`);
	}
	if (pin.anchor && pin.element_offset) {
		const box = measure(pin.anchor);
		if (_isMeasurable(box)) {
			const x = box.left + (pin.element_offset.dx_pct / 100) * box.width;
			const y = box.top + (pin.element_offset.dy_pct / 100) * box.height;
			return _resolved('element', pin, pin.anchor, x, y, viewport);
		}
	}
	for (const near of pin.nearby_anchors ?? []) {
		const box = measure(near.selector);
		if (_isMeasurable(box)) {
			return _resolved('anchor', pin, near.selector, box.left + near.dx_px, box.top + near.dy_px, viewport);
		}
	}
	const x = (pin.x_pct / 100) * viewport.w;
	const y = (pin.y_pct / 100) * viewport.h;
	return { ..._resolved('viewport', pin, null, x, y, viewport), x_pct: pin.x_pct, y_pct: pin.y_pct };
}

/** CSS for a resolved marker. The viewport tier keeps the percent form pins
 * always used, so an old pin's style string is byte-identical to before. */
export function resolvedPinStyle(pin: PositionedPin, pos: ResolvedPinPosition): string {
	if (pos.tier === 'viewport') return `left:${pin.x_pct}%;top:${pin.y_pct}%`;
	return `left:${Math.round(pos.x * 10) / 10}px;top:${Math.round(pos.y * 10) / 10}px`;
}

// ----- capture --------------------------------------------------------------
export interface AnchorCandidate {
	selector: string;
	rect: PinRect;
}

const round1 = (v: number): number => Math.round(v * 10) / 10;
const round2 = (v: number): number => Math.round(v * 100) / 100;

/**
 * The placement record for a click at `point` (viewport px). `primary` is the
 * element `anchor` names, already confirmed to be the clicked lineage; the
 * offset is only recorded when the click is inside its box.
 */
export function buildPinPlacement(
	point: { x: number; y: number },
	primary: AnchorCandidate | null,
	neighbours: readonly AnchorCandidate[]
): PinPlacement {
	let element_offset: PinElementOffset | null = null;
	if (primary !== null && _isMeasurable(primary.rect)) {
		const r = primary.rect;
		const inside =
			point.x >= r.left && point.x <= r.left + r.width && point.y >= r.top && point.y <= r.top + r.height;
		if (inside) {
			element_offset = {
				dx_pct: round2(((point.x - r.left) / r.width) * 100),
				dy_pct: round2(((point.y - r.top) / r.height) * 100)
			};
		}
	}
	const seen = new Set<string>(primary !== null ? [primary.selector] : []);
	const ranked: { near: PinNearbyAnchor; dist: number; area: number }[] = [];
	for (const c of neighbours) {
		if (seen.has(c.selector) || !_isMeasurable(c.rect)) continue;
		seen.add(c.selector);
		const dx = point.x - c.rect.left;
		const dy = point.y - c.rect.top;
		ranked.push({
			near: { selector: c.selector, dx_px: round1(dx), dy_px: round1(dy) },
			dist: Math.hypot(dx, dy),
			area: c.rect.width * c.rect.height
		});
	}
	// Smallest offset first: the offset is what gets re-applied, so the
	// shorter it is the less a layout change can throw the pin off.
	ranked.sort((a, b) => a.dist - b.dist || a.area - b.area);
	return { element_offset, nearby_anchors: ranked.slice(0, MAX_NEARBY_ANCHORS).map((r) => r.near) };
}

/** A parked draft's placement, or null when absent or half-shaped. */
export function parsePinPlacement(raw: unknown): PinPlacement | null {
	if (typeof raw !== 'object' || raw === null) return null;
	const rec = raw as Record<string, unknown>;
	const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
	let element_offset: PinElementOffset | null = null;
	if (rec.element_offset !== null && rec.element_offset !== undefined) {
		const eo = rec.element_offset as Record<string, unknown>;
		if (typeof eo !== 'object' || !num(eo.dx_pct) || !num(eo.dy_pct)) return null;
		element_offset = { dx_pct: eo.dx_pct, dy_pct: eo.dy_pct };
	}
	if (!Array.isArray(rec.nearby_anchors)) return null;
	const nearby_anchors: PinNearbyAnchor[] = [];
	for (const n of rec.nearby_anchors as unknown[]) {
		const a = n as Record<string, unknown> | null;
		if (typeof a !== 'object' || a === null) return null;
		if (typeof a.selector !== 'string' || !num(a.dx_px) || !num(a.dy_px)) return null;
		nearby_anchors.push({ selector: a.selector, dx_px: a.dx_px, dy_px: a.dy_px });
	}
	if (nearby_anchors.length > MAX_NEARBY_ANCHORS) return null;
	return { element_offset, nearby_anchors };
}

// ----- browser shell ----------------------------------------------------------
const CSS_SAFE_ID = /^[A-Za-z][\w-]*$/;
const ATTR_SAFE = /^[^"\\\n]+$/;

/** The stable selector for ONE element, in the shapes parseAnchorSelector
 * accepts, or null when it carries nothing stable. Never a class. */
export function stableSelectorOf(el: Element): string | null {
	if (el.id && CSS_SAFE_ID.test(el.id)) return `#${el.id}`;
	const testId = el.getAttribute('data-testid');
	if (testId && ATTR_SAFE.test(testId)) return `[data-testid="${testId}"]`;
	const label = el.getAttribute('aria-label');
	if (label && ATTR_SAFE.test(label)) return `${el.tagName.toLowerCase()}[aria-label="${label}"]`;
	return null;
}

/** The one element a stored selector names, or null for none/several. */
export function findUniqueBySelector(selector: string, root: ParentNode): Element | null {
	const css = parseAnchorSelector(selector);
	if (css === null) return null;
	const matches = root.querySelectorAll(css);
	return matches.length === 1 ? matches[0] : null;
}

/** Live box of the one element `selector` names; the resolver's `measure`. */
export function measureSelector(selector: string): PinRect | null {
	if (typeof document === 'undefined') return null;
	const el = findUniqueBySelector(selector, document);
	if (el === null) return null;
	const r = el.getBoundingClientRect();
	return { left: r.left, top: r.top, width: r.width, height: r.height };
}

function _candidate(el: Element, selector: string | null): AnchorCandidate | null {
	if (selector === null || isPinSystemElement(el)) return null;
	if (typeof document === 'undefined' || findUniqueBySelector(selector, document) !== el) return null;
	const r = el.getBoundingClientRect();
	return { selector, rect: { left: r.left, top: r.top, width: r.width, height: r.height } };
}

/**
 * Capture a placement for a click at `point` over `hit`, whose primary
 * selector (describeAnchor's) is `anchor`. Walks the hit's ancestors and
 * their siblings for stable, page-unique neighbours.
 */
export function capturePinPlacement(
	point: { x: number; y: number },
	hit: Element | null,
	anchor: string | null
): PinPlacement {
	if (hit === null || typeof document === 'undefined') return { element_offset: null, nearby_anchors: [] };
	let primary: AnchorCandidate | null = null;
	if (anchor !== null) {
		const el = findUniqueBySelector(anchor, document);
		if (el !== null && el.contains(hit)) primary = _candidate(el, anchor);
	}
	const neighbours: AnchorCandidate[] = [];
	const visit = (el: Element | null): void => {
		if (el === null || el === document.body || el === document.documentElement) return;
		const c = _candidate(el, stableSelectorOf(el));
		if (c !== null) neighbours.push(c);
	};
	let node: Element | null = hit;
	for (let depth = 0; node !== null && depth < NEARBY_WALK_ANCESTORS; depth += 1) {
		visit(node);
		let prev = node.previousElementSibling;
		let next = node.nextElementSibling;
		for (let i = 0; i < NEARBY_WALK_SIBLINGS; i += 1) {
			visit(prev);
			visit(next);
			prev = prev?.previousElementSibling ?? null;
			next = next?.nextElementSibling ?? null;
		}
		node = node.parentElement;
	}
	return buildPinPlacement(point, primary, neighbours);
}
