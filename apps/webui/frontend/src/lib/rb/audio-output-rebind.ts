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
 *
 * THE COOLDOWN DEFERS, IT DOES NOT DISCARD. Wed 9 Sep 2026 17:44:43Z on the
 * Air, from the unified log of the packaged app's `com.apple.WebKit.GPU`:
 * the system default output flipped `BuiltInSpeakerDevice` -> Bluetooth
 * `A4-C1-39-53-B0-BE` at 17:44:43.5, and BACK to `BuiltInSpeakerDevice` at
 * 17:44:45.4 - a 1.9s flap, well inside `REBIND_COOLDOWN_MS`. The first edge
 * re-bound the graph onto headphones that were already going away; the second
 * edge, the one naming the device the operator actually ended up listening on,
 * hit the cooldown and was DROPPED - no cycle, no perf row, no toast, nothing
 * armed to retry. Audio stayed dead until the app was restarted. The same
 * 2-second flap signature appears at 17:16:59 / 17:17:01Z the same afternoon,
 * which is what makes this the recurring cutout rather than a one-off.
 *
 * The cooldown's job is to stop a flap LOOPING the graph, not to lose the
 * device the flap settles on. So a request that arrives inside the cooldown is
 * held and re-runs the moment the cooldown expires, coalesced to one pending
 * request (latest reason wins). The rate limit is unchanged: still at most one
 * suspend/resume per `REBIND_COOLDOWN_MS`.
 *
 * TWO MORE WAYS TO DROP THE SETTLING EDGE, both closed here. A rebind is a pair
 * of awaits, and a device that is disappearing is exactly the device whose
 * `suspend()`/`resume()` is slow - so the cycle routinely outlives the 400ms
 * debounce. (a) A request whose debounce fires while `rebinding` is still true
 * used to return at the top and vanish; it is now held and re-run when the
 * active cycle finishes. (b) A request already accepted and held used to be
 * re-gated on `isAnyDeckPlaying()` when it ran, so an operator pausing during
 * the cooldown discarded it. Nothing repairs that afterwards: `_resumeContext()`
 * only calls `resume()` on a SUSPENDED context, and this failure keeps the
 * context `running`. The playing gate therefore applies when a request LANDS,
 * never again when it runs.
 *
 * LANDS means the trigger, not the end of the debounce. There are three delays
 * between a device-change edge and its suspend/resume - the 400ms debounce, an
 * in-flight cycle, and the 10s cooldown - and playback state sampled after any
 * of them is sampled too late. `request()` therefore captures acceptance when
 * it is called and carries it through all three.
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
	/**
	 * `severity` is passed through so a FAILED rebind can leave the browser.
	 * Every row here used to be recorded at `info`, and `recordPerfEvent` only
	 * escalates at `error` - so `audio-output-rebind-failed`, the one row that
	 * means "the operator is hearing nothing and we could not fix it", never
	 * reached the engine's client-error log. That is why the 17:44Z cutout above
	 * left no trace in `webui-client-errors-2026-09-09.log`.
	 */
	recordPerfEvent(kind: string, message: string, severity?: 'info' | 'warn' | 'error'): void;
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
	let pendingAccepted = false;
	let deferred: unknown = null;
	let deferredReason = '';
	let heldDuringRebind = false;

	/**
	 * Park `reason` in the single deferred slot, armed to run in `delayMs`.
	 * Latest reason wins and an already-armed timer is left alone, so N held
	 * requests still cost exactly one extra cycle.
	 */
	function holdRequest(reason: string, delayMs: number): void {
		deferredReason = reason;
		if (deferred !== null) return;
		deferred = effects.setTimeout(() => {
			deferred = null;
			void rebind(deferredReason, true);
		}, delayMs);
	}

	/**
	 * `accepted` marks a request that ALREADY passed the playing gate and is now
	 * being re-run out of the deferred slot. Re-gating it would silently discard
	 * a held request whenever the operator pauses during the cooldown, and
	 * resuming cannot repair that: `_resumeContext()` in `audio-engine.svelte.ts`
	 * only calls `resume()` on a SUSPENDED context, and the whole shape of this
	 * cutout is a context that stays `running` on a dead output stream.
	 */
	async function rebind(reason: string, accepted = false): Promise<void> {
		// Gated on LANDING only: nothing playing means there is no audio to save,
		// so there is nothing worth holding for later either. Deferring an idle
		// request would arm a cycle that fires 10s after the operator stopped, on
		// a graph that no longer needs one.
		if (!accepted && !isAnyDeckPlaying()) return;
		if (rebinding) {
			// HELD, never dropped. A suspend/resume that outlives the 400ms
			// debounce used to swallow every request landing during it - the same
			// drop the cooldown branch below exists to stop, one state earlier. No
			// timer is armed here: the in-flight cycle re-runs this reason from its
			// own `finally`, which then takes the cooldown branch and waits out
			// whatever is left of the cooldown.
			deferredReason = reason;
			heldDuringRebind = true;
			effects.recordPerfEvent(
				'audio-output-rebind-deferred',
				`${reason} arrived while a rebind was still in flight; ` +
					'held rather than dropped',
				'warn'
			);
			return;
		}
		const sinceLastMs = effects.now() - lastRebindAt;
		if (sinceLastMs < REBIND_COOLDOWN_MS) {
			// HELD, never dropped: see the 17:44:43Z Bluetooth flap in the module
			// docstring. One pending request at a time, latest reason wins, so a
			// device flapping ten times still costs exactly one extra cycle.
			holdRequest(reason, REBIND_COOLDOWN_MS - sinceLastMs);
			effects.recordPerfEvent(
				'audio-output-rebind-deferred',
				`${reason} arrived ${Math.round(sinceLastMs)}ms into the ` +
					`${REBIND_COOLDOWN_MS}ms cooldown; held rather than dropped`,
				'warn'
			);
			return;
		}
		rebinding = true;
		lastRebindAt = effects.now();
		try {
			if (ctx.state === 'running') await ctx.suspend();
			await ctx.resume();
			effects.recordPerfEvent('audio-output-rebound', `output re-bound after ${reason}`, 'info');
			effects.pushToast(`Audio output re-bound (${reason})`, 'info');
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			// `error`, so `recordPerfEvent` escalates it to the engine's
			// client-error log. A silent audio failure recorded only in this
			// browser's ring is a silent audio failure.
			effects.recordPerfEvent('audio-output-rebind-failed', `${reason}: ${message}`, 'error');
			effects.pushToast(`Audio output could not be re-bound after ${reason}: ${message}`, 'error');
		} finally {
			rebinding = false;
			if (heldDuringRebind) {
				heldDuringRebind = false;
				// Re-entry lands in the cooldown branch above and arms the residual
				// wait, so the rate limit is untouched: still one cycle per cooldown.
				void rebind(deferredReason, true);
			}
		}
	}

	function request(reason: string): Promise<void> {
		pendingReason = reason;
		// Playback is sampled HERE, at the trigger, not after the debounce. The
		// request lands when the device changes; an operator pausing inside the
		// 400ms window must not discard a trigger that was live when it arrived.
		// This is the same defect as the cooldown and in-flight cases, one state
		// earlier - the debounce is simply the FIRST delay between a trigger and
		// its cycle, and every one of them has to carry the acceptance forward.
		// Sticky across the coalescing window: if any request folded into this
		// batch was accepted, the batch is accepted.
		pendingAccepted = pendingAccepted || isAnyDeckPlaying();
		if (pending !== null) effects.clearTimeout(pending);
		return new Promise<void>((resolve) => {
			pending = effects.setTimeout(() => {
				pending = null;
				const accepted = pendingAccepted;
				pendingAccepted = false;
				// `accepted === false` still re-checks the gate inside `rebind`, so a
				// device change that lands idle and is followed by playback starting
				// inside the debounce is honoured exactly as before.
				void rebind(pendingReason, accepted).then(resolve);
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
			pending = null;
			pendingAccepted = false;
			// The deferred cycle outlives the debounce timer and would otherwise
			// suspend/resume a context this route already closed, which is the
			// spurious-error-toast failure the stall listener test pins.
			if (deferred !== null) effects.clearTimeout(deferred);
			deferred = null;
			// Also clears the timerless in-flight hold, which would otherwise be
			// re-armed by the running cycle's `finally` after this teardown.
			heldDuringRebind = false;
		}
	};
}
