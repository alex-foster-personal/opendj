import { parsePinsVisible, serializePinsVisible, PINS_VISIBLE_KEY } from './feedback';

const listeners = new Set<() => void>();

/** Browser-only persistence boundary for the comment-pin visibility preference. */
export function readPinsVisible(storage: Storage): boolean {
	return parsePinsVisible(storage.getItem(PINS_VISIBLE_KEY));
}

export function writePinsVisible(storage: Storage, visible: boolean): void {
	storage.setItem(PINS_VISIBLE_KEY, serializePinsVisible(visible));
	for (const fn of listeners) fn();
}

/** Same-tab listener when another surface toggles pin visibility. */
export function onPinsVisibleChanged(fn: () => void): () => void {
	listeners.add(fn);
	return () => listeners.delete(fn);
}
