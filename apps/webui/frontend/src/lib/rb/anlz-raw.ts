/**
 * Keep a deck's ANLZ payload out of the deep `$state` proxy (perf board row
 * `anlz-raw-not-deep-proxy`, performance-register #5597(a)).
 *
 * `deckStates` is a deep `$state` proxy, so assigning a plain ANLZ object to
 * `st.anlz` proxies every beat, cue, phrase and waveform band: one Source
 * signal per touched element, roughly 20 MB and ~100k signals per loaded deck,
 * and every per-frame beat lookup registers dependencies on them.
 *
 * Svelte 5's `proxy()` returns any object whose prototype is not
 * Object.prototype or Array.prototype unchanged. Re-parenting the payload onto
 * a private prototype (which itself inherits Object.prototype, so
 * hasOwnProperty, toString and JSON behave as before) makes the assignment
 * store the raw object. `st.anlz` itself stays a tracked property, so
 * replacing it still re-renders every reader. The trade-off: an in-place edit
 * of the payload (anlz.x = ..., anlz.cues.push(...)) would no longer notify
 * anyone. Nothing does that today: every writer replaces the whole object,
 * and tests/unit/anlz-raw-deck-state.test.mjs scans src for any `.anlz =`
 * writer that skips this wrapper or any in-place payload edit.
 *
 * A spread copy (`{ ...st.anlz, beatgrid }`) is a fresh Object.prototype
 * object again, so a writer that merges must wrap its result too, or that
 * deck goes back to being deep-proxied.
 */

import type { AnlzData } from './anlz-types';

// Branded through the global symbol registry so isRawAnlz() agrees across
// separately bundled copies of this module (unit tests load one per bundle).
const ANLZ_RAW_BRAND = Symbol.for('opendj.anlz-raw');
const ANLZ_RAW_PROTO: object = Object.create(Object.prototype, {
	[ANLZ_RAW_BRAND]: { value: true }
});

/** Returns `anlz` (same identity) made non-proxyable. Null passes through. */
export function rawAnlz<T extends AnlzData | null | undefined>(anlz: T): T {
	if (anlz === null || anlz === undefined) return anlz;
	if (Object.getPrototypeOf(anlz) !== Object.prototype) return anlz;
	try {
		Object.setPrototypeOf(anlz, ANLZ_RAW_PROTO);
	} catch {
		// Already a $state proxy (its trap forbids prototype changes) or a
		// frozen object: leave it as is, behavior matches the old path.
	}
	return anlz;
}

/** True when `anlz` has been published raw (test and devtools probe). */
export function isRawAnlz(anlz: unknown): boolean {
	if (typeof anlz !== 'object' || anlz === null) return false;
	const proto: unknown = Object.getPrototypeOf(anlz);
	return (
		typeof proto === 'object' &&
		proto !== null &&
		(proto as Record<symbol, unknown>)[ANLZ_RAW_BRAND] === true
	);
}
