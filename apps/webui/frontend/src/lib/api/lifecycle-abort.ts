/**
 * Document lifecycle: in-flight boot fetches reject when navigation tears the
 * page down; those failures must not surface as user-visible errors.
 *
 * Discriminator is unload ordering (pagehide / beforeunload in capture phase),
 * not exception message text -- see issue #8817.
 */

let goingAway = false;
/** In-flight client navigations tear down the same fetches as a full unload. */
let softNavigationHandoffs = 0;
/** Date.now() at the latest unload signal, null until one fires. */
let goingAwayAtMs: number | null = null;

export function isPageGoingAway(): boolean {
	return goingAway || softNavigationHandoffs > 0;
}

/** Call before `goto` / `navigate`; run the returned end callback after the route settles. */
export function beginSoftNavigationHandoff(): () => void {
	softNavigationHandoffs += 1;
	return () => {
		softNavigationHandoffs = Math.max(0, softNavigationHandoffs - 1);
	};
}

/**
 * Milliseconds since the latest unload signal, or null when none has fired.
 *
 * For a caller that silences more than its own fetch: a signal does not prove
 * the page left (a download or an external-protocol link fires beforeunload and
 * the document lives on), so a wide silence is bounded by time, not left on for
 * the rest of the session.
 */
export function msSincePageGoingAway(): number | null {
	return goingAwayAtMs === null ? null : Date.now() - goingAwayAtMs;
}

/** Test hook: simulate document teardown without depending on browser events. */
export function markPageGoingAwayForTests(): void {
	goingAway = true;
	goingAwayAtMs = Date.now();
}

export function _resetPageGoingAwayForTests(): void {
	goingAway = false;
	softNavigationHandoffs = 0;
	goingAwayAtMs = null;
}

/** Register capture-phase listeners so the flag is set before fetch rejections run. */
export function installPageGoingAwayListener(target: EventTarget): void {
	const onGoingAway = (): void => {
		goingAway = true;
		goingAwayAtMs = Date.now();
	};
	target.addEventListener('pagehide', onGoingAway, true);
	target.addEventListener('beforeunload', onGoingAway, true);
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
	installPageGoingAwayListener(window);
}
