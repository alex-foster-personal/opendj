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

/** Delays before each resume attempt, in order. The first is immediate. */
export const CONTEXT_RESUME_BACKOFF_MS: readonly number[] = [0, 150, 400, 1_000, 2_500, 5_000];

/** The states that mean audio has stopped coming out. */
const NON_RUNNING = new Set(['suspended', 'interrupted', 'closed']);

export interface ContextWatchdogEffects {
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfTiming(kind: string, stages: Record<string, number>): void;
	sleep(ms: number): Promise<void>;
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

	async function recover(state: string): Promise<void> {
		// Re-entrancy guard: a successful resume fires statechange from INSIDE
		// ctx.resume(), and a second loop started from there would double every
		// attempt and race the first loop's own exit check.
		if (recovering) return;
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
		void recover(state);
	}

	ctx.addEventListener('statechange', onStateChange);
	return () => ctx.removeEventListener('statechange', onStateChange);
}
