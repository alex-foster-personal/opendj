import { parsePinsVisible, serializePinsVisible, PINS_VISIBLE_KEY } from './feedback';

/** Browser-only persistence boundary for the comment-pin visibility preference. */
export function readPinsVisible(storage: Storage): boolean {
	return parsePinsVisible(storage.getItem(PINS_VISIBLE_KEY));
}

export function writePinsVisible(storage: Storage, visible: boolean): void {
	storage.setItem(PINS_VISIBLE_KEY, serializePinsVisible(visible));
}
