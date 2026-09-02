/**
 * Re-bind the AudioContext to the live output device after the device changed
 * under it.
 *
 * Wed 2 Sep 2026 18:01 CEST: a phone call took the maintainer's Bluetooth headphones for
 * a second (HFP profile), gave them back, and Open DJ stayed silent. The
 * evidence: a 176 ms callback gap, then `presentation-clock-stalled` (the HAL
 * output position stopped advancing), and NO `statechange`: Chromium keeps the
 * context `running` on a dead output stream, so the statechange watchdog never
 * fired and the silence watchdog measures the graph, not the device. WebKit
 * reports `interrupted` for the same event and is already handled.
 *
 * The recovery is a suspend/resume cycle, which makes the browser re-open its
 * output stream on the current default device. Two triggers, both debounced and
 * under one cooldown so a flapping device cannot loop the graph:
 *   - `navigator.mediaDevices` `devicechange` while a deck is playing
 *   - the presentation clock reporting a stall (device position stopped)
 * Only the transition is acted on; the statechange watchdog owns anything that
 * leaves the context non-running afterwards.
 */

export const REBIND_DEBOUNCE_MS = 400;
export const REBIND_COOLDOWN_MS = 10_000;

export interface RebindableAudioContext {
	readonly state: string;
	suspend(): Promise<void>;
	resume(): Promise<void>;
}

export interface RebindEffects {
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfEvent(kind: string, message: string): void;
	now(): number;
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

export interface OutputRebindHandle {
	/** Ask for a rebind (debounced, cooldown-guarded). Returns after the cycle. */
	request(reason: string): Promise<void>;
	uninstall(): void;
}

type StallListener = (deck: number) => void;
let _stallListeners: StallListener[] = [];

/** Called by the presentation clock reporter on the stall EDGE (false -> true). */
export function noteOutputStall(deck: number): void {
	for (const listener of _stallListeners) listener(deck);
}

export function installOutputRebind(
	ctx: RebindableAudioContext,
	effects: RebindEffects,
	isAnyDeckPlaying: () => boolean,
	mediaDevices: { addEventListener(t: 'devicechange', h: () => void): void; removeEventListener(t: 'devicechange', h: () => void): void } | null
): OutputRebindHandle {
	let rebinding = false;
	let lastRebindAt = Number.NEGATIVE_INFINITY;
	let pending: unknown = null;
	let pendingReason = '';

	async function rebind(reason: string): Promise<void> {
		if (rebinding) return;
		if (effects.now() - lastRebindAt < REBIND_COOLDOWN_MS) return;
		if (!isAnyDeckPlaying()) return;
		rebinding = true;
		lastRebindAt = effects.now();
		try {
			if (ctx.state === 'running') await ctx.suspend();
			await ctx.resume();
			effects.recordPerfEvent('audio-output-rebound', `output re-bound after ${reason}`);
			effects.pushToast(`Audio output re-bound (${reason})`, 'info');
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			effects.recordPerfEvent('audio-output-rebind-failed', `${reason}: ${message}`);
			effects.pushToast(`Audio output could not be re-bound after ${reason}: ${message}`, 'error');
		} finally {
			rebinding = false;
		}
	}

	function request(reason: string): Promise<void> {
		pendingReason = reason;
		if (pending !== null) effects.clearTimeout(pending);
		return new Promise<void>((resolve) => {
			pending = effects.setTimeout(() => {
				pending = null;
				void rebind(pendingReason).then(resolve);
			}, REBIND_DEBOUNCE_MS);
		});
	}

	const onDeviceChange = (): void => {
		void request('output device changed');
	};
	const onStall: StallListener = (deck) => {
		void request(`deck ${deck} output position stalled`);
	};
	mediaDevices?.addEventListener('devicechange', onDeviceChange);
	_stallListeners.push(onStall);

	return {
		request,
		uninstall(): void {
			mediaDevices?.removeEventListener('devicechange', onDeviceChange);
			_stallListeners = _stallListeners.filter((l) => l !== onStall);
			if (pending !== null) effects.clearTimeout(pending);
		}
	};
}
