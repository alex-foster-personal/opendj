/**
 * Document lifecycle: in-flight boot fetches reject when navigation tears the
 * page down; those failures must not surface as user-visible errors.
 *
 * Discriminator is unload ordering (pagehide / beforeunload in capture phase),
 * not exception message text -- see issue #8817.
 */

let goingAway = false;

export function isPageGoingAway(): boolean {
	return goingAway;
}

/** Test hook: simulate document teardown without depending on browser events. */
export function markPageGoingAwayForTests(): void {
	goingAway = true;
}

export function _resetPageGoingAwayForTests(): void {
	goingAway = false;
}

/** Register capture-phase listeners so the flag is set before fetch rejections run. */
export function installPageGoingAwayListener(target: EventTarget): void {
	const onGoingAway = (): void => {
		goingAway = true;
	};
	target.addEventListener('pagehide', onGoingAway, true);
	target.addEventListener('beforeunload', onGoingAway, true);
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
	installPageGoingAwayListener(window);
}
