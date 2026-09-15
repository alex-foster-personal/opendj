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

let _pendingReleases: Array<() => void> = [];

/** Drain callback: release every caller waiting on the owed decode start. */
export async function resumeEagerStemDecodeOwedJob(): Promise<void> {
	const releases = _pendingReleases;
	_pendingReleases = [];
	for (const release of releases) release();
}

/** Requests a shed slot; resolves once released (immediately if not deferred). */
export function requestEagerStemDecodeSlot(shed: BackgroundDemandShed): Promise<void> {
	return new Promise((resolve) => {
		_pendingReleases.push(resolve);
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

/** No-op (decode proceeds immediately) until a shed is armed. */
export async function awaitEagerStemDecodeSlot(): Promise<void> {
	if (_shed === null) return;
	await requestEagerStemDecodeSlot(_shed);
}
