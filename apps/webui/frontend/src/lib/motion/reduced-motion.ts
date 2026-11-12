/** The one reduced-motion policy (A11Y-11811): OS `(prefers-reduced-motion: reduce)` OR
 * the `reduce_motion` ui-pref; `app.css` zeros non-essential durations under
 * `html[data-reduce-motion]` and the media query; `.motion-essential` opts progress bars
 * and meters out (WCAG 2.3.3). */

export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
export const REDUCE_MOTION_ATTR = 'data-reduce-motion';

let _pref = false;

/** The `reduce_motion` ui-pref as last applied by the root layout. */
export function setReduceMotionPref(on: boolean): void {
	if (typeof on !== 'boolean') {
		throw new TypeError(`reduce_motion pref must be a boolean, got ${typeof on}`);
	}
	_pref = on;
}

function _mediaMatches(): boolean {
	if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
		return false;
	}
	return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}

/** True when the OS asks for reduced motion or the user turned it on in Appearance.
 * With an argument it is the pure truth table (tests); without, it reads the live media query and the pref. */
export function prefersReducedMotion(input?: { media: boolean; pref: boolean }): boolean {
	if (input !== undefined) {
		if (typeof input.media !== 'boolean' || typeof input.pref !== 'boolean') {
			throw new TypeError('prefersReducedMotion input members must be booleans');
		}
		return input.media || input.pref;
	}
	return _mediaMatches() || _pref;
}

/** Set or clear html[data-reduce-motion] from the live policy. */
export function applyReducedMotionAttr(root?: Pick<Element, 'toggleAttribute'>): boolean {
	let el = root;
	if (el === undefined) {
		if (typeof document === 'undefined') return prefersReducedMotion();
		el = document.documentElement;
	}
	if (typeof el.toggleAttribute !== 'function') return prefersReducedMotion();
	const on = prefersReducedMotion();
	el.toggleAttribute(REDUCE_MOTION_ATTR, on);
	return on;
}

/** Call onChange when the OS preference flips. Returns the unsubscribe; a no-op without matchMedia. */
export function watchReducedMotionMedia(onChange: () => void): () => void {
	if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
		return () => {};
	}
	const mq = window.matchMedia(REDUCED_MOTION_QUERY);
	const handler = () => onChange();
	mq.addEventListener('change', handler);
	return () => mq.removeEventListener('change', handler);
}
