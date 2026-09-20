/**
 * Computer-as-decks knob control: selection, global scroll, inverse link pairs
 * with bass-preserving stagger, and central drag/scroll sensitivity.
 *
 * Shift+click selects a dial (global wheel nudges it). Plain click/drag does
 * not select. Alt/Option+click two knobs to link (turn one down => other up).
 */

import { detectWheelInputKind, scaledWheelStep } from './wheel-adjust';

// ----- central sensitivity (tweak here; not buried in components) -----

export const KNOB_CFG = {
	/** Vertical drag pixels for a full 0..1 sweep (higher = less sensitive). */
	dragVerticalPx: 120,
	/** Horizontal drag pixels for a full 0..1 sweep (much larger = fine adjust). */
	dragHorizontalPx: 520,
	/** Wheel deltaY scale -> knob units (hover or selected). Scaled per input
	 * kind by WHEEL_SENSITIVITY, so a trackpad burst is not 3 steps a flick. */
	scrollStep: 0.028,
	/** Arrow-key nudge. */
	keyStep: 0.02,
	/** Pointer travel (px, per axis) a press may wobble and still be a click.
	 * Mouse jitter and touch contact motion both emit pointermove with no intent. */
	clickSlopPx: 4,
	/**
	 * Extra boost applied to the *rising* side of a linked pair, as a
	 * fraction of |delta|. Keeps bass energy from dipping while the cut/boost
	 * crosses through the middle.
	 */
	linkStagger: 0.18
} as const;

/** True once a press has travelled past the click slop on either axis. */
export function pointerTravelIsDrag(dx: number, dy: number): boolean {
	return Math.abs(dx) > KNOB_CFG.clickSlopPx || Math.abs(dy) > KNOB_CFG.clickSlopPx;
}

// ----- types -----

export type KnobRole =
	| 'trim'
	| 'high'
	| 'mid'
	| 'low'
	| 'filter'
	| 'hp-mix'
	| 'hp-level'
	| 'stem-vocal'
	| 'stem-instrumental'
	| 'stem-drums';

export interface KnobRef {
	id: string;
	getValue: () => number;
	setValue: (value: number) => void;
}

export interface LinkedPair {
	a: string;
	b: string;
}

// ----- state -----

export const knobUi = $state<{
	selectedId: string | null;
	linkPendingId: string | null;
	link: LinkedPair | null;
	hoveredId: string | null;
}>({
	selectedId: null,
	linkPendingId: null,
	link: null,
	hoveredId: null
});

/**
 * Per-id STACK, not a single ref: EqOverlay's knobs (LIBUX-05 cmd+E) mount
 * the same deck:band id as the mixer's always-mounted ChannelStrip knob for
 * the same parameter while raised, then unmount on close. A single-ref map
 * loses the mixer's registration permanently on that unregister. Register
 * pushes, unregister pops its own entry, so closing the overlay always
 * uncovers whichever control was registered before it - LIFO, which matches
 * mount order since EqOverlay is strictly nested inside the mixer's lifetime.
 */
const _registry = new Map<string, KnobRef[]>();
let _wheelBound = false;

function _activeRef(id: string): KnobRef | undefined {
	return _registry.get(id)?.at(-1);
}

// ----- helpers -----

export function knobId(deckId: number | 'hp', role: KnobRole): string {
	return `${deckId}:${role}`;
}

export function clamp01(v: number): number {
	return Math.min(1, Math.max(0, v));
}

/** Inverse link with stagger on the rising side (bass-preserving cross). */
export function applyLinkedDelta(
	primary: number,
	secondary: number,
	delta: number,
	stagger: number = KNOB_CFG.linkStagger
): { primary: number; secondary: number } {
	let p = clamp01(primary + delta);
	let s = clamp01(secondary - delta);
	const rise = Math.abs(delta) * stagger;
	if (delta > 0) p = clamp01(p + rise);
	else if (delta < 0) s = clamp01(s + rise);
	return { primary: p, secondary: s };
}

function _partnerOf(id: string): string | null {
	const link = knobUi.link;
	if (link === null) return null;
	if (link.a === id) return link.b;
	if (link.b === id) return link.a;
	return null;
}

// ----- registry -----

export function registerKnob(ref: KnobRef): void {
	const stack = _registry.get(ref.id);
	if (stack) stack.push(ref);
	else _registry.set(ref.id, [ref]);
	_ensureGlobalWheel();
}

export function unregisterKnob(id: string): void {
	const stack = _registry.get(id);
	if (!stack) return;
	stack.pop();
	if (stack.length > 0) return;
	_registry.delete(id);
	// An earlier registrant for this id (e.g. the mixer knob a shadowing
	// EqOverlay widget sits on top of) surviving underneath means the user's
	// selection/link is still pointing at a live widget - only clear the
	// UI-state once the LAST registrant for this id is gone.
	if (knobUi.selectedId === id) knobUi.selectedId = null;
	if (knobUi.linkPendingId === id) knobUi.linkPendingId = null;
	if (knobUi.hoveredId === id) knobUi.hoveredId = null;
	if (knobUi.link !== null && (knobUi.link.a === id || knobUi.link.b === id)) {
		knobUi.link = null;
	}
}

/** Test-only: clears the module-level registry stack between test cases. */
export function _resetKnobRegistryForTests(): void {
	_registry.clear();
}

export function isKnobSelected(id: string): boolean {
	return knobUi.selectedId === id;
}

export function isKnobLinked(id: string): boolean {
	return _partnerOf(id) !== null || knobUi.linkPendingId === id;
}

export function isKnobHighlighted(id: string): boolean {
	return isKnobSelected(id) || isKnobLinked(id);
}

export function setKnobHovered(id: string | null): void {
	knobUi.hoveredId = id;
}

export function selectKnob(id: string): void {
	knobUi.selectedId = id;
}

/** Shift+click: select for global wheel (toggle off if already selected). */
export function shiftClickKnob(id: string): void {
	knobUi.selectedId = knobUi.selectedId === id ? null : id;
}

/** Alt/Option+click: arm link on first dial, complete on second, clear if same. */
export function altClickKnob(id: string): void {
	if (knobUi.linkPendingId === null) {
		knobUi.linkPendingId = id;
		return;
	}
	if (knobUi.linkPendingId === id) {
		knobUi.linkPendingId = null;
		knobUi.link = null;
		return;
	}
	knobUi.link = { a: knobUi.linkPendingId, b: id };
	knobUi.linkPendingId = null;
}

export function clearKnobLink(): void {
	knobUi.link = null;
	knobUi.linkPendingId = null;
}

/** Apply a delta to a knob; if linked, move the partner inversely with stagger. */
export function nudgeKnob(id: string, delta: number): void {
	if (!Number.isFinite(delta) || delta === 0) return;
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const partnerId = _partnerOf(id);
	if (partnerId === null) {
		primary.setValue(clamp01(primary.getValue() + delta));
		return;
	}
	const secondary = _activeRef(partnerId);
	if (secondary === undefined) {
		primary.setValue(clamp01(primary.getValue() + delta));
		return;
	}
	const next = applyLinkedDelta(primary.getValue(), secondary.getValue(), delta);
	primary.setValue(next.primary);
	secondary.setValue(next.secondary);
}

/** Set absolute value on primary; derive delta vs current for link math. */
export function setKnobAbsolute(id: string, value: number): void {
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const delta = clamp01(value) - primary.getValue();
	nudgeKnob(id, delta);
}

/**
 * Drag helper: recompute from pointer-down baselines so stagger does not
 * accumulate across pointermove frames.
 */
export function setKnobFromDrag(
	id: string,
	startPrimary: number,
	targetPrimary: number,
	partnerId: string | null,
	startSecondary: number | null
): void {
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const target = clamp01(targetPrimary);
	if (partnerId === null || startSecondary === null) {
		primary.setValue(target);
		return;
	}
	const secondary = _activeRef(partnerId);
	if (secondary === undefined) {
		primary.setValue(target);
		return;
	}
	const delta = target - startPrimary;
	const next = applyLinkedDelta(startPrimary, startSecondary, delta);
	primary.setValue(next.primary);
	secondary.setValue(next.secondary);
}

export function linkedPartnerId(id: string): string | null {
	return _partnerOf(id);
}

export function readKnobValue(id: string): number | null {
	return _activeRef(id)?.getValue() ?? null;
}

function _onWindowWheel(e: WheelEvent): void {
	const id = knobUi.selectedId;
	if (id === null) return;
	if (!_registry.has(id)) return;
	// Ignore when a text field owns focus.
	const t = e.target;
	if (t instanceof HTMLElement) {
		const tag = t.tagName;
		if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) return;
	}
	e.preventDefault();
	const dir = e.deltaY > 0 ? -1 : e.deltaY < 0 ? 1 : 0;
	if (dir === 0) return;
	// This listener is the one wheel-adjustable path that does NOT run through
	// use:wheelAdjust, so the trackpad scaling has to be applied explicitly here
	// or the shift-selected dial stays hypersensitive while every other control
	// is calm. Same seam, same constant.
	const step = scaledWheelStep(KNOB_CFG.scrollStep, detectWheelInputKind(e));
	nudgeKnob(id, dir * step);
}

function _ensureGlobalWheel(): void {
	if (_wheelBound || typeof window === 'undefined') return;
	window.addEventListener('wheel', _onWindowWheel, { passive: false });
	_wheelBound = true;
}
