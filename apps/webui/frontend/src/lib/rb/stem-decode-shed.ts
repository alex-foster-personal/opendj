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
 * that return. The bounded hold below gives it a promise instead: push a
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

/**
 * Component-scope bridge (same nullable-setter pattern as
 * setSilenceDropoutHandler in app-init.ts): null until app-init.ts arms the
 * background demand shed, so stem-graph.ts never imports app-init.ts or the
 * shed singleton directly.
 */
let _shed: BackgroundDemandShed | null = null;
let _kernelPressure: (() => boolean) | null = null;

/** `kernelPressure` reads whether the kernel itself reports memory pressure
 * (PERFMODE-18); without it only an xrun counts as real pressure. */
export function setEagerStemDecodeShed(
	shed: BackgroundDemandShed | null,
	kernelPressure: (() => boolean) | null = null
): void {
	_shed = shed;
	_kernelPressure = shed === null ? null : kernelPressure;
}

/**
 * STEM-47, graded by PERFMODE-18: the longest a deck's OWN stem decode is
 * held back, by how real the pressure is.
 *
 * The shed has no upper bound of its own: it releases when no deck is playing
 * or the pressure clears. The DJ loaded this track onto a deck, so its stems
 * are wanted work, not speculative work: the hold is a short courtesy to the
 * audio thread, then the decode starts regardless.
 *
 * The bound was a flat 6 s. Measured Thu 1 Oct 2026: a host whose churn score
 * sits at 10 to 25 times the early-warning threshold for a whole session,
 * with the kernel at level 1, deferred every decode and released none, so
 * each paid all 6 s. Pressure is polled every 10 s, so a hold of a few
 * seconds almost never sees it clear; what the hold buys is distance from
 * the play-start transient. Two bounds, one number each:
 *
 *   REAL pressure (the kernel reports level 2 or above): 2 s. Long enough for the transport start and the
 *   glitch that tripped it to pass, short enough that the stem buttons are
 *   live before a DJ who pressed play reaches for them.
 *
 *   EARLY WARNING only (churn over its threshold, or an xrun in the current
 *   window, with the kernel below level 2): 500 ms. PERF-STEMDEC-04 measured
 *   the xrun signal true on 8 of 10 loads on a loaded host with no effect on
 *   late callbacks inside the decode, so it no longer selects the 2 s bound. Nothing has been damaged; the hold only steps the decode off the
 *   play dispatch and its schedule lead.
 */
export const EAGER_STEM_DECODE_MAX_DEFER_MS = 2000;
export const EAGER_STEM_DECODE_EARLY_WARNING_DEFER_MS = 500;

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
	let release: ((cause: _ReleaseCause) => void) | null = null;
	const slot = new Promise<_ReleaseCause>((resolve) => {
		release = (released) => {
			cause = released;
			resolve(released);
		};
		_pendingReleases.push(release);
	});
	// A shed that does not defer runs the resumer inside request(), so `cause`
	// is already set when request() returns; anything else is a real hold.
	_shed.request('eager-stem-decode');
	if (cause !== null) return 'immediate';
	// Read at the moment of the hold: the grade is the pressure that caused it.
	const boundMs =
		_kernelPressure?.() === true
			? EAGER_STEM_DECODE_MAX_DEFER_MS
			: EAGER_STEM_DECODE_EARLY_WARNING_DEFER_MS;
	wait.onDeferred?.();
	const setTimer = wait.setTimer ?? ((run: () => void, ms: number) => setTimeout(run, ms));
	const clearTimer =
		wait.clearTimer ?? ((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>));
	let handle: unknown = null;
	const bound = new Promise<'timed_out'>((resolve) => {
		handle = setTimer(() => resolve('timed_out'), wait.maxDeferMs ?? boundMs);
	});
	const start = await Promise.race([slot, bound]);
	clearTimer(handle);
	// A timed-out decode is already running: it is no longer held, so a later
	// release must neither count it nor call it.
	if (start === 'timed_out') _pendingReleases = _pendingReleases.filter((r) => r !== release);
	return start;
}
