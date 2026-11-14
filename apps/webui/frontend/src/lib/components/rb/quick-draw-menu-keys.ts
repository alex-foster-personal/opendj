// #14632: the quick draw menu's keyboard, WAI-ARIA menu pattern (mimicked).
// Pure: the component flattens its menu items into one list (contextual
// actions, then the quick-draw roots, then the open leaf) and passes which of
// them are enabled; this decides where focus goes or what the key does.

export type QuickDrawMenuKeyAction =
	{ kind: 'focus'; index: number } | { kind: 'activate' } | { kind: 'close' };

/** Index of the first enabled item, or -1 when no item is enabled. */
export function firstEnabledIndex(enabled: readonly boolean[]): number {
	return enabled.indexOf(true);
}

/** Index of the last enabled item, or -1 when no item is enabled. */
export function lastEnabledIndex(enabled: readonly boolean[]): number {
	return enabled.lastIndexOf(true);
}

function _stepEnabled(enabled: readonly boolean[], from: number, step: 1 | -1): number {
	const n = enabled.length;
	for (let hop = 1; hop <= n; hop++) {
		const i = (((from + step * hop) % n) + n) % n;
		if (enabled[i]) return i;
	}
	return -1;
}

function _focus(index: number): QuickDrawMenuKeyAction | null {
	return index < 0 ? null : { kind: 'focus', index };
}

/** Maps a keydown on a focused menu item to a focus move, an activation or a close; null leaves the key alone. */
export function quickDrawMenuKeyAction(
	key: string,
	current: number,
	enabled: readonly boolean[]
): QuickDrawMenuKeyAction | null {
	switch (key) {
		case 'ArrowDown':
			return _focus(current < 0 ? firstEnabledIndex(enabled) : _stepEnabled(enabled, current, 1));
		case 'ArrowUp':
			return _focus(current < 0 ? lastEnabledIndex(enabled) : _stepEnabled(enabled, current, -1));
		case 'Home':
			return _focus(firstEnabledIndex(enabled));
		case 'End':
			return _focus(lastEnabledIndex(enabled));
		case 'Enter':
		case ' ':
			return current >= 0 && enabled[current] === true ? { kind: 'activate' } : null;
		case 'Escape':
		case 'Tab':
			return { kind: 'close' };
		default:
			return null;
	}
}
