/** Keyboard selection helpers for the settings results list. */

export function clampIndex(index: number, length: number): number {
	if (length <= 0) return -1;
	if (index < 0) return 0;
	if (index >= length) return length - 1;
	return index;
}

/** ArrowUp/Down (and optional wrap). Returns next index or -1 when empty. */
export function moveSelection(
	current: number,
	delta: number,
	length: number,
	wrap = false
): number {
	if (length <= 0) return -1;
	if (current < 0) return delta >= 0 ? 0 : length - 1;
	let next = current + delta;
	if (wrap) {
		next = ((next % length) + length) % length;
	} else {
		next = clampIndex(next, length);
	}
	return next;
}

export type BooleanKeyAction = 'toggle' | 'on' | 'off' | null;

/** Space/Enter -> toggle; ArrowRight -> on; ArrowLeft -> off. */
export function booleanKeyAction(key: string): BooleanKeyAction {
	if (key === ' ' || key === 'Spacebar' || key === 'Enter') return 'toggle';
	if (key === 'ArrowRight') return 'on';
	if (key === 'ArrowLeft') return 'off';
	return null;
}

export function applyBooleanAction(current: boolean, action: BooleanKeyAction): boolean | null {
	if (action === 'toggle') return !current;
	if (action === 'on') return true;
	if (action === 'off') return false;
	return null;
}
