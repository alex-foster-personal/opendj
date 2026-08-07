/**
 * Mouse-wheel adjust for continuous 0..1 controls (dials and faders).
 *
 * One Svelte action, applied to a control's interactive element, so every
 * dial and fader gains scroll-to-adjust without each component growing its
 * own wheel handler. Wheel up raises, wheel down lowers.
 *
 * Direction-only, not magnitude-scaled: a DJ needs one notch to mean the
 * same amount every time, and trackpad deltaY magnitudes vary wildly
 * between devices and momentum phases.
 *
 * Requirements:
 *   ✔︎ Scrolling a control changes it without scrolling the page.
 *     [if] the pointer is over a dial and the wheel turns [then] the value
 *       moves by exactly one step and the panel underneath does not scroll
 *   ✔︎ Values stay inside the control's 0..1 domain.
 *     [if] the wheel keeps turning at either end [then] the value pins to
 *       0 or 1 and never leaves the range ⛔️
 *   ✔︎ Inert / read-only controls ignore the wheel entirely.
 *     [if] a control passes disabled [then] no set() call is made and the
 *       page scroll is left alone
 */

/** Per-control-type wheel step, in 0..1 control units. */
export const WHEEL_STEP = {
	/** Dials: trim, EQ bands, filter, headphone mix/level. */
	knob: 0.028,
	/** Channel level faders. */
	fader: 0.028,
	/** Crossfader - coarser, it travels a shorter useful distance. */
	crossfader: 0.04,
	/** Pitch fader: 0.01 of full travel = 0.16% at +-8, 0.32% at +-16. */
	pitch: 0.01
} as const;

export interface WheelAdjustParams {
	/** Value change per wheel notch, in 0..1 units. Must be finite and > 0. */
	step: number;
	/** Current value, read at wheel time so it is never stale. */
	get: () => number;
	/** Receives the clamped next value. */
	set: (value: number) => void;
	/** Inert controls opt out and let the page scroll normally. */
	disabled?: boolean;
}

function _clamp01(value: number): number {
	return Math.min(1, Math.max(0, value));
}

function _assertParams(params: WheelAdjustParams): void {
	if (!Number.isFinite(params.step) || params.step <= 0) {
		throw new RangeError(`wheelAdjust step must be a positive finite number, got ${params.step}`);
	}
	if (typeof params.get !== 'function' || typeof params.set !== 'function') {
		throw new TypeError('wheelAdjust requires get() and set() functions');
	}
}

/** Wheel direction as -1 / 0 / +1. Vertical wins; deltaX covers shift-scroll. */
export function wheelDirection(event: WheelEvent): -1 | 0 | 1 {
	const delta = event.deltaY !== 0 ? event.deltaY : event.deltaX;
	if (delta < 0) return 1;
	if (delta > 0) return -1;
	return 0;
}

export function wheelAdjust(node: Element, params: WheelAdjustParams) {
	_assertParams(params);
	let current = params;

	function onWheel(event: WheelEvent): void {
		if (current.disabled === true) return;
		const direction = wheelDirection(event);
		if (direction === 0) return;
		event.preventDefault();
		event.stopPropagation();
		const next = _clamp01(current.get() + direction * current.step);
		current.set(next);
	}

	node.addEventListener('wheel', onWheel as EventListener, { passive: false });

	return {
		update(next: WheelAdjustParams): void {
			_assertParams(next);
			current = next;
		},
		destroy(): void {
			node.removeEventListener('wheel', onWheel as EventListener);
		}
	};
}
