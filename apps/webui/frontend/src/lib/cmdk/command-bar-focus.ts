/**
 * Keyboard focus return when the v2 Cmd K bar closes (issue #12947, CMDK-14411).
 * Mirrors the opener capture in `focus-trap.ts` without Tab interception.
 */

export interface FocusableElement {
	isConnected: boolean;
	focus(): void;
	blur?(): void;
}

function isFocusable(el: unknown): el is FocusableElement & HTMLElement {
	return (
		typeof el === 'object' &&
		el !== null &&
		'focus' in el &&
		typeof (el as HTMLElement).focus === 'function'
	);
}

/** The focused control outside the bar when Cmd K opens, if any. */
export function captureCommandBarOpener(
	active: unknown,
	barRoot: HTMLElement | null
): HTMLElement | null {
	if (!isFocusable(active) || active === document.body) return null;
	if (barRoot?.contains(active)) return null;
	return active;
}

/** After the bar unmounts, return focus to the opener or blur a trapped control. */
export function restoreCommandBarFocus(
	opener: HTMLElement | null,
	barRoot: HTMLElement | null
): void {
	const active = document.activeElement;
	const insideBar =
		active !== null && active !== document.body && (barRoot?.contains(active) ?? false);
	const restore = active === null || active === document.body || insideBar;
	if (!restore) return;

	if (opener?.isConnected) {
		opener.focus();
		return;
	}
	if (isFocusable(active) && active !== document.body && typeof active.blur === 'function') {
		active.blur();
	}
}
