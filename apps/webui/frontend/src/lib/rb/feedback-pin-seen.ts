import { parsePinSeen, serializePinSeen, PIN_SEEN_KEY, type PinSeen } from './feedback';

/** Browser-only persistence boundary for the pin unread/seen stamp, mirroring
 * feedback-pin-visibility.ts. Split out of FeedbackWidget.svelte (Thu 3 Sep
 * 2026, pin review v2) to bring that file back under the 600-line file-size
 * gate; a fail-fast caller must still wrap the read (a blocked store here
 * must not be swallowed into a silent "everything unread" default). */
export function readPinSeen(storage: Storage): PinSeen {
	return parsePinSeen(storage.getItem(PIN_SEEN_KEY));
}

export function writePinSeen(storage: Storage, next: PinSeen): void {
	storage.setItem(PIN_SEEN_KEY, serializePinSeen(next));
}
