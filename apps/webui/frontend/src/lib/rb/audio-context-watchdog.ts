/**
 * P0: an AudioContext that stops running while a deck is playing must be
 * noticed, said out loud, and recovered from.
 *
 * On Wed 2 Sep 2026 the output device was lost and re-found underneath a
 * running context while the machine was in swap thrash. Audio stopped for ~24
 * minutes and the app displayed nothing, because the engine's only
 * `statechange` listener acted on the `running` state and let `suspended`,
 * `interrupted` (the state WebKit uses for exactly this) and `closed` fall
 * through in silence. `_resumeContext()` existed but ran only from an explicit
 * `play()`, so a context that died mid-playback was resumed by nothing. Audio
 * came back only when the maintainer changed a macOS system setting by hand.
 *
 * WHY EVERY EFFECT IS INJECTED. The recovery loop is a schedule of sleeps, and
 * a test that waited out real timers would be measuring this machine's spare
 * CPU rather than the schedule - on a machine that is deliberately kept in
 * thrash. `Effects` is the same shape as cc-failover's: one object, so a test
 * drives the real decision path with nothing performed.
 */

/**
 * THE SCHEDULE IS BOUNDED, AND THAT BOUND IS WHY IT NEEDS RE-ARMING.
 *
 * The backoff below is deliberately finite: an unbounded resume loop on the
 * main thread is a second way to lose the audio it is trying to save, and
 * `audio-context-lifecycle.test.mjs` pins that. But finite means it RUNS OUT,
 * and until Wed 9 Sep 2026 running out was terminal. The whole schedule spans
 * 9,050 ms; `statechange` fires on TRANSITIONS, so a context that goes
 * `interrupted` and STAYS `interrupted` fires exactly once, the six attempts
 * fail against a device that is still gone, and nothing ever asks again. Not
 * the liveness poll, which returns `idle` for any context that is not
 * `running` and so cannot be the backstop; not `_resumeContext()`, which runs
 * only from an explicit `play()`. The operator gets one toast and permanent
 * silence.
 *
 * Nine seconds is shorter than the things that cause this. A Bluetooth
 * re-pair, a phone call, a screen lock, a packaged WKWebView backgrounded
 * behind another window: all routinely outlast it.
 *
 * So recovery is RE-ARMED rather than extended. `noteRecoveryOpportunity` is
 * called on the edges that carry new information -- the page becoming visible
 * again, the device list changing -- and each one re-runs the same bounded
 * schedule from the top. The rate limit is intact: still at most
 * `CONTEXT_RESUME_BACKOFF_MS.length` attempts per opportunity, still no
 * free-running timer, and an opportunity that arrives while the context is
 * already running or nothing is playing costs one comparison. The difference
 * is only that "the device came back" is now a question that can be asked more
 * than once.
 */

/** Delays before each resume attempt, in order. The first is immediate. */
export const CONTEXT_RESUME_BACKOFF_MS: readonly number[] = [0, 150, 400, 1_000, 2_500, 5_000];

type RecoveryListener = (reason: string) => void;
let _recoveryListeners: RecoveryListener[] = [];

/**
 * Tell every armed watchdog that something changed which might mean the output
 * device is back. Fans out like `noteOutputStall`, and for the same reason: the
 * edges live in the DOM (`visibilitychange`, `devicechange`) and this module
 * stays device-agnostic and testable without one.
 */
export function noteRecoveryOpportunity(reason: string): void {
	for (const listener of _recoveryListeners) listener(reason);
}

/** The states that mean audio has stopped coming out. */
const NON_RUNNING = new Set(['suspended', 'interrupted', 'closed']);

export interface ContextWatchdogEffects {
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfTiming(kind: string, stages: Record<string, number>): void;
	sleep(ms: number): Promise<void>;
	noteUnexpectedPause(state: string): void;
}

/** Only what this module reads, so a test needs no real AudioContext. */
export interface WatchableAudioContext {
	readonly state: string;
	resume(): Promise<void>;
	addEventListener(type: 'statechange', handler: () => void): void;
	removeEventListener(type: 'statechange', handler: () => void): void;
}

function _describe(state: string): string {
	if (state === 'interrupted') {
		return 'the audio device was taken away (interrupted)';
	} else if (state === 'suspended') {
		return 'audio output was suspended';
	} else if (state === 'closed') {
		return 'the audio graph was closed';
	}
	return `audio output entered an unexpected state (${state})`;
}

/**
 * Watch `ctx` and recover it while `isAnyDeckPlaying()` says it matters.
 *
 * Returns a detach function. Recovery is deliberately NOT attempted when
 * nothing is playing: every context starts suspended until a gesture resumes
 * it, so alarming there would train the operator to ignore the channel before
 * the set has even started.
 */
export function installAudioContextWatchdog(
	ctx: WatchableAudioContext,
	effects: ContextWatchdogEffects,
	isAnyDeckPlaying: () => boolean
): () => void {
	let recovering = false;
	let heldOpportunity = false;

	async function recover(state: string): Promise<void> {
		// Re-entrancy guard: a successful resume fires statechange from INSIDE
		// ctx.resume(), and a second loop started from there would double every
		// attempt and race the first loop's own exit check.
		//
		// HELD, NEVER DROPPED (Sol P1 BLOCKING on PR #1644). Returning here used
		// to discard the arrival outright, which is the SAME defect #1619 fixed
		// one module over: the window between the final failed `resume()` and
		// `recovering` clearing is exactly when a device coming back announces
		// itself, and a `devicechange` landing in it was the only new signal
		// there was ever going to be -- no further `statechange` follows a
		// context that never changed state. One flag, not a queue: N edges
		// during one schedule are worth exactly one more schedule.
		if (recovering) {
			heldOpportunity = true;
			return;
		}
		recovering = true;
		try {
			for (const delayMs of CONTEXT_RESUME_BACKOFF_MS) {
				await effects.sleep(delayMs);
				if (ctx.state === 'running') return;
				try {
					await ctx.resume();
				} catch {
					// Expected on a closed context and on a device that is still
					// gone. The loop is the retry; a throw here is not the end of it.
				}
			}
			if (ctx.state !== 'running') {
				effects.pushToast(
					`Audio did not come back after ${CONTEXT_RESUME_BACKOFF_MS.length} attempts ` +
						`(${_describe(state)}). Check the system output device.`,
					'error'
				);
			}
		} finally {
			recovering = false;
			if (heldOpportunity) {
				heldOpportunity = false;
				// Re-gated on the CONTEXT, never again on PLAYBACK (Codex P1
				// BLOCKING, discussion_r3973882771). This was #1619's defect a
				// third time. A held edge only ever gets here by having passed
				// the playing gate at its own call site, so it was ACCEPTED
				// while a deck was playing; re-asking here discarded it whenever
				// the operator paused between the edge landing and the schedule
				// finishing -- and pausing is precisely what an operator DOES
				// when the sound stops. The playing gate applies when an
				// opportunity LANDS, never again when it runs, exactly as
				// `installOutputRebind`'s `accepted` flag says one module over.
				//
				// Losing it is not repairable from the UI: pressing play reaches
				// `_resumeContext()`, which resumes a `suspended` or
				// `interrupted` context -- but only if a play() ever comes, and
				// an operator who paused because there was no sound has no
				// reason to press it. The re-armed schedule is the only thing
				// that recovers a context nobody presses play on.
				//
				// The give-up toast that a failed re-arm may raise is left
				// UNGATED on purpose: it is the same incident the operator was
				// already alarmed about while playing, it is still true, and a
				// paused operator finding out now beats finding out at the next
				// drop.
				//
				// The CONTEXT check stays. A context that came back on its own
				// makes the held edge worth nothing, and that is the one
				// re-check that cannot lose anything.
				if (NON_RUNNING.has(ctx.state)) void recover(ctx.state);
			}
		}
	}

	function onStateChange(): void {
		const state = ctx.state;
		const running = state === 'running';
		effects.recordPerfTiming('audio-context', { context_running: running ? 1 : 0 });
		if (running || !NON_RUNNING.has(state)) return;
		if (!isAnyDeckPlaying()) return;
		// The state name travels in the message on purpose: "something went wrong
		// with audio" is not actionable in the middle of a set.
		effects.pushToast(
			`Audio stopped: ${_describe(state)} [${state}]. Trying to recover...`,
			'error'
		);
		effects.noteUnexpectedPause(state);
		void recover(state);
	}

	/**
	 * A second door into the SAME bounded recovery, opened by an outside edge
	 * rather than by a state transition.
	 *
	 * Gated on exactly what `onStateChange` is gated on, and deliberately
	 * silent: this fires on every device change and every tab focus, so a toast
	 * here would be noise. The alarm already went out when the context dropped;
	 * this is the retry, not a new incident. `recover`'s own re-entrancy guard
	 * means an opportunity landing mid-schedule is a no-op rather than a second
	 * concurrent loop.
	 */
	const onRecoveryOpportunity: RecoveryListener = (): void => {
		if (ctx.state === 'running' || !NON_RUNNING.has(ctx.state)) return;
		if (!isAnyDeckPlaying()) return;
		// The bare STATE, not a composed sentence: `recover` passes this to
		// `_describe`, which matches the three state names exactly and otherwise
		// falls through to "an unexpected state (...)". Handing it "interrupted
		// (retrying after the window became visible)" would put that whole string
		// inside the fallback and give the operator a garbled toast at the one
		// moment they need a clear one.
		void recover(ctx.state);
	};

	ctx.addEventListener('statechange', onStateChange);
	_recoveryListeners.push(onRecoveryOpportunity);
	return () => {
		ctx.removeEventListener('statechange', onStateChange);
		// Dropped by identity, exactly like the rebind module's stall listener:
		// `noteRecoveryOpportunity` fans out to EVERY registered listener, so one
		// left behind by a route unmount would keep trying to resume a context
		// that has been closed, forever, once per device change.
		_recoveryListeners = _recoveryListeners.filter((l) => l !== onRecoveryOpportunity);
		// Also clears the timerless hold, which the running schedule's `finally`
		// would otherwise re-arm against a context this route already closed --
		// the same teardown the rebind module's `heldDuringRebind` needs.
		heldOpportunity = false;
	};
}
