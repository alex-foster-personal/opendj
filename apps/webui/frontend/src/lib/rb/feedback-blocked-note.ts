/** Split blocked notes without losing the source offset of their first sentence. */
function blockedPinRequestEnd(note: string): number {
	const end = note.search(/[.!?](?:\s|$)/);
	return end === -1 ? note.length : end + 1;
}

/** The actionable first sentence of a blocked note, displayed above its detail. */
export function blockedPinRequest(note: string): string {
	return note.slice(0, blockedPinRequestEnd(note)).trim();
}

/** Supporting detail after a blocked pin's actionable first sentence. */
export function blockedPinDetail(note: string): string {
	return note.slice(blockedPinRequestEnd(note)).trim();
}
