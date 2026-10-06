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
// One controller PER stem download, registered while it runs and aborted the
// moment a block begins, so stem work already in flight stops instead of
// finishing (Codex review, PR #4039).
//
// Never one shared, page-lifetime controller (P0, Tue 6 Oct 2026, regressed in
// 935752ef33): WebKit keeps every fetch's following signal, and with it the
// Response, alive for as long as the source signal lives, so a shared
// controller retained every stem fetch of the session (heap with empty decks:
// 2,951 AbortSignal, 733 signalFollow, 2,243 FetchResponse).
const _inFlight = new Set<AbortController>();

/** Block stem decode until the returned release is called. The release only
 * clears its OWN block, so a stale release cannot unblock a newer holder. */
export function blockStemDecode(reason: string): () => void {
	if (reason.trim() === '') throw new Error('blockStemDecode: reason must be non-empty');
	if (_block !== null) {
		throw new Error(`blockStemDecode: stem decode is already blocked (${_block.reason})`);
	}
	const block: StemDecodeBlock = { reason };
	_block = block;
	const cancelled = [..._inFlight];
	_inFlight.clear();
	for (const controller of cancelled) {
		controller.abort(new DOMException(`stem work cancelled: ${reason}`, 'AbortError'));
	}
	return () => {
		if (_block !== block) return;
		_block = null;
	};
}

/** The reason stem decode is blocked, or null when decks may use stems. */
export function stemDecodeBlockReason(): string | null {
	return _block === null ? null : _block.reason;
}

/** A fresh signal for ONE stem download: already aborted while stems are
 * blocked, and aborted when a block begins mid-download. The caller MUST call
 * `end()` in `finally`, which drops the registration so nothing outlives it. */
export function beginStemWork(): { signal: AbortSignal; end: () => void } {
	const controller = new AbortController();
	if (_block !== null) {
		controller.abort(new DOMException(`stem work cancelled: ${_block.reason}`, 'AbortError'));
	} else {
		_inFlight.add(controller);
	}
	return { signal: controller.signal, end: () => void _inFlight.delete(controller) };
}

/** How many stem downloads are registered right now (0 when none is running). */
export function inFlightStemWorkCount(): number {
	return _inFlight.size;
}
