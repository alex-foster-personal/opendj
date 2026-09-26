/**
 * Whether decks may probe, fetch, decode or adopt stems right now (PERFMODE-15).
 *
 * Trackify is a listening player with no stems. The engine's lazy stem upgrade
 * (STEM-37) is reached from more than one place (every load, and the graph
 * rebuild after an output stall), so the mode switches it off HERE, once, and
 * the engine reads this at every stem entry point instead of each caller
 * remembering to opt out.
 *
 * One holder at a time. A second block while one is held is a lifecycle bug
 * (two Trackify sessions at once), so it throws rather than stacking.
 */

interface StemDecodeBlock {
	readonly reason: string;
}

let _block: StemDecodeBlock | null = null;

/** Block stem decode until the returned release is called. The release only
 * clears its OWN block, so a stale release cannot unblock a newer holder. */
export function blockStemDecode(reason: string): () => void {
	if (reason.trim() === '') throw new Error('blockStemDecode: reason must be non-empty');
	if (_block !== null) {
		throw new Error(`blockStemDecode: stem decode is already blocked (${_block.reason})`);
	}
	const block: StemDecodeBlock = { reason };
	_block = block;
	return () => {
		if (_block === block) _block = null;
	};
}

/** The reason stem decode is blocked, or null when decks may use stems. */
export function stemDecodeBlockReason(): string | null {
	return _block === null ? null : _block.reason;
}
