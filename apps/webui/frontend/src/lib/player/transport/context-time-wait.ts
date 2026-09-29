/**
 * Wait until an AudioContext's clock reaches a target time, giving up when the
 * clock stops advancing. Moved verbatim from audio-engine.svelte.ts: it touches
 * no engine state, only the context it is handed.
 */

import { CONTEXT_WAIT_POLL_MS, CONTEXT_WAIT_STALL_TIMEOUT_MS } from '$lib/player/constants';

export interface ContextTimeSource {
	readonly currentTime: number;
	readonly state: string;
}

/**
 * The two time primitives the context-time wait below is built on: the
 * millisecond reading it measures stall progress against, and the sleep it
 * parks on between polls. They are injectable for one reason - the wait's
 * contract is "give up within `stallTimeoutMs` of the last observed
 * progress", and that is a statement about scheduling arithmetic, not about
 * how punctually a loaded machine delivers a timer callback. A test that
 * drives a virtual clock checks the arithmetic; a test that times real
 * `setTimeout` calls checks the host's spare CPU.
 */
export interface ContextWaitClock {
	nowMs(): number;
	sleep(ms: number): Promise<void>;
}

export const REAL_CONTEXT_WAIT_CLOCK: ContextWaitClock = {
	nowMs: () => Date.now(),
	sleep: (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))
};

export async function waitForAdvancingContextTime(
	ctx: ContextTimeSource,
	targetContextTime: number,
	stillCurrent: () => boolean = () => true,
	stallTimeoutMs: number = CONTEXT_WAIT_STALL_TIMEOUT_MS,
	clock: ContextWaitClock = REAL_CONTEXT_WAIT_CLOCK
): Promise<void> {
	if (!Number.isFinite(targetContextTime) || targetContextTime < 0) {
		throw new RangeError(
			`targetContextTime must be finite and non-negative, got ${targetContextTime}`
		);
	}
	if (!Number.isFinite(stallTimeoutMs) || stallTimeoutMs <= 0) {
		throw new RangeError(`stallTimeoutMs must be finite and positive, got ${stallTimeoutMs}`);
	}
	const initialContextTime = ctx.currentTime;
	if (!Number.isFinite(initialContextTime) || initialContextTime < 0) {
		throw new RangeError(
			`AudioContext time must be finite and non-negative, got ${initialContextTime}`
		);
	}
	let lastContextTime = initialContextTime;
	let lastProgressAtMs = clock.nowMs();
	while (ctx.currentTime < targetContextTime) {
		if (!stillCurrent()) throw new Error('context-time wait state changed before target');
		if (ctx.state !== 'running') {
			throw new Error(`AudioContext is not running during context-time wait; got ${ctx.state}`);
		}
		const contextTime = ctx.currentTime;
		if (!Number.isFinite(contextTime) || contextTime < lastContextTime) {
			throw new Error(
				`AudioContext time must be finite and monotonic, got ${contextTime} after ${lastContextTime}`
			);
		}
		if (contextTime > lastContextTime) {
			lastContextTime = contextTime;
			lastProgressAtMs = clock.nowMs();
		}
		const stallRemainingMs = stallTimeoutMs - (clock.nowMs() - lastProgressAtMs);
		if (stallRemainingMs <= 0) {
			throw new Error(
				`AudioContext time stalled before target ${targetContextTime} at ${contextTime}`
			);
		}
		const contextRemainingMs = (targetContextTime - contextTime) * 1000;
		const delayMs = Math.max(
			1,
			Math.ceil(Math.min(CONTEXT_WAIT_POLL_MS, contextRemainingMs, stallRemainingMs))
		);
		await clock.sleep(delayMs);
	}
}
