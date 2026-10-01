/**
 * PERFMODE-04 shed job for eager stem decode.
 *
 * decodeStemBuffers (stem-graph.ts) runs AFTER a deck is already playable on
 * its mix buffer (Q18), so deferring it never blocks audible playback - it
 * only delays when stem controls become available.
 *
 * The registered `run` is the RESUMER, not the worker (same shape as
 * cloudsync-scheduler-shed.ts): createBackgroundDemandShed's request(id)
 * non-deferred branch does `void run()` with no return value back to the
 * caller, so a caller that needs to know when it may proceed cannot rely on
 * that return. requestEagerStemDecodeSlot gives it a promise instead: push a
 * resolver, then request the shed slot. If the shed does not defer, request()
 * invokes resumeEagerStemDecodeOwedJob synchronously-ish, which drains the
 * resolver right away. If it defers, the resolver waits for the eventual
 * drain (shed.sync() on the next pressure tick after signals clear).
 */

import type { BackgroundDemandShed } from '$lib/rb/playing-gate';

/** How a held decode came to start: the shed released it, or the DJ did. */
type _ReleaseCause = 'released' | 'forced';
let _pendingReleases: Array<(cause: _ReleaseCause) => void> = [];

function _releaseAll(cause: _ReleaseCause): number {
	const releases = _pendingReleases;
	_pendingReleases = [];
	for (const release of releases) release(cause);
	return releases.length;
}

/** Drain callback: release every caller waiting on the owed decode start. */
export async function resumeEagerStemDecodeOwedJob(): Promise<void> {
	_releaseAll('released');
}

/** STEM-47: the DJ asked for the stems now. Releases every held decode and
 * returns how many were waiting (0 means nothing was held). */
export function releaseEagerStemDecodeNow(): number {
	return _releaseAll('forced');
}

/** Requests a shed slot; resolves once released (immediately if not deferred). */
export function requestEagerStemDecodeSlot(shed: BackgroundDemandShed): Promise<void> {
	return new Promise((resolve) => {
		_pendingReleases.push(() => resolve());
		shed.request('eager-stem-decode');
	});
}

/**
 * Component-scope bridge (same nullable-setter pattern as
 * setSilenceDropoutHandler in app-init.ts): null until app-init.ts arms the
 * background demand shed, so stem-graph.ts never imports app-init.ts or the
 * shed singleton directly.
 */
let _shed: BackgroundDemandShed | null = null;

export function setEagerStemDecodeShed(shed: BackgroundDemandShed | null): void {
	_shed = shed;
}

/**
 * STEM-47: the longest a deck's OWN stem decode is held back by pressure.
 *
 * The shed has no upper bound of its own: it releases when no deck is playing
 * or the pressure clears. On a machine that stays under pressure through a
 * whole set, that left a playing deck's stem buttons dead for the whole track.
 * The DJ loaded this track onto a deck, so its stems are wanted work, not
 * speculative work: the hold is a short courtesy to the audio thread, then the
 * decode starts regardless. Named so the trade is one number in one place.
 */
export const EAGER_STEM_DECODE_MAX_DEFER_MS = 6000;

/** `immediate`: never held. `released`: the shed let it go. `forced`: the DJ
 * asked for it. `timed_out`: held for the full bound, then started anyway. */
export type EagerStemDecodeStart = 'immediate' | 'released' | 'forced' | 'timed_out';

export interface EagerStemDecodeWait {
	/** Called once, synchronously, if the decode is held back. */
	onDeferred?: () => void;
	maxDeferMs?: number;
	setTimer?: (run: () => void, ms: number) => unknown;
	clearTimer?: (handle: unknown) => void;
}

/** Resolves when the decode may start, saying why. `immediate` (no hold) until
 * a shed is armed. A hold never outlives `maxDeferMs`. */
export async function awaitEagerStemDecodeSlot(
	wait: EagerStemDecodeWait = {}
): Promise<EagerStemDecodeStart> {
	if (_shed === null) return 'immediate';
	let cause: _ReleaseCause | null = null;
	const slot = new Promise<_ReleaseCause>((resolve) => {
		_pendingReleases.push((released) => {
			cause = released;
			resolve(released);
		});
	});
	// A shed that does not defer runs the resumer inside request(), so `cause`
	// is already set when request() returns; anything else is a real hold.
	_shed.request('eager-stem-decode');
	if (cause !== null) return 'immediate';
	wait.onDeferred?.();
	const setTimer = wait.setTimer ?? ((run: () => void, ms: number) => setTimeout(run, ms));
	const clearTimer =
		wait.clearTimer ?? ((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>));
	let handle: unknown = null;
	const bound = new Promise<'timed_out'>((resolve) => {
		handle = setTimer(() => resolve('timed_out'), wait.maxDeferMs ?? EAGER_STEM_DECODE_MAX_DEFER_MS);
	});
	const start = await Promise.race([slot, bound]);
	clearTimer(handle);
	return start;
}
