/**
 * LATENCY-03 / S1 / Q2: everything the audio graph MEASURES about its context.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5
 * (docs/perf/performance-register.md): instrumentation lives in its own module
 * and the engine keeps only the call sites, so the formulas can be read, tested
 * and changed without touching the file that owns playback.
 *
 * Nothing here may block or throw into the audio path. Both entry points are
 * called from graph construction and from the resume path, where a failure to
 * measure must never become a failure to play.
 */

import {
	installAudioContextWatchdog,
	type WatchableAudioContext
} from '$lib/rb/audio-context-watchdog';
import { recordPerfEvent, recordPerfTiming } from '$lib/rb/perf-event-log';
import { pushToast } from '$lib/stores.svelte';
import {
	flushWorkletAckWindow,
	resetWorkletAckStats
} from '$lib/rb/worklet-ack-stats';
import {
	detachXrunSentinel,
	installXrunSentinel,
	installXrunSessionGlobal
} from '$lib/rb/xrun-sentinel';

/**
 * The context whose authoritative (running) device-floor row has been emitted.
 *
 * Held as the context ITSELF rather than a boolean, so a rebuilt graph is a new
 * object and therefore a new stamp by construction: a sticky flag would let a
 * fresh context keep quoting the previous device's floors.
 */
let _stampedRunningContext: AudioContext | null = null;

/**
 * LATENCY-03: stamp this machine's device floors onto the perf ring, so a
 * `scheduled_offset_ms` read later is never compared across machines by
 * accident.
 *
 * Emitted TWICE, because the build-time reading is not trustworthy on its own:
 * `AudioContext.outputLatency` reads 0.00 while the context is SUSPENDED, and a
 * context is suspended at construction until a user gesture resumes it.
 * Verified: a freshly built, not-yet-resumed context reports
 * `baseLatency=5.80ms outputLatency=0.00ms state=suspended`, i.e. the row
 * silently under-reports the device floor by the whole 32ms output term. So the
 * build stamp is kept (it is the only reading available if the context never
 * runs) and a second, authoritative stamp is taken on the FIRST observation of
 * a running context. `context_running` says which is which.
 *
 * Per-schedule rows are unaffected: they already read both floors live at emit
 * time and carry their own copies. This is the one-time row only.
 */
export function stampContextDeviceFloors(ctx: AudioContext): void {
	const running = ctx.state === 'running';
	if (running && _stampedRunningContext === ctx) return;
	if (running) _stampedRunningContext = ctx;
	recordPerfTiming('audio-context', {
		sample_rate_hz: ctx.sampleRate,
		base_latency_ms: Math.round(ctx.baseLatency * 1e6) / 1000,
		output_latency_ms: Math.round(ctx.outputLatency * 1e6) / 1000,
		// 0 means outputLatency above may be a suspended-context zero, not a floor.
		context_running: running ? 1 : 0
	});
}

/**
 * S1 / Q2: arm the audio-thread glitch detector for this context.
 *
 * Fire-and-forget because `addModule` is async and the graph build is not: the
 * decks must not wait on an instrument. Every failure path is RECORDED rather
 * than swallowed - a sentinel that quietly failed to load would leave the app
 * reporting zero xruns forever, which is indistinguishable from a healthy
 * machine and is the worst possible failure for a counter whose entire job is
 * to say "this one is not healthy". Audio is never blocked either way.
 */
export function armXrunSentinel(ctx: AudioContext): void {
	installXrunSessionGlobal();
	void installXrunSentinel(ctx).catch((error: unknown) => {
		recordPerfEvent(
			'xrun-sentinel-failed',
			`the xrun sentinel did not start, so this session counts no glitches: ${String(error)}`
		);
	});
}

/**
 * Stand every instrument down with the graph that carried it.
 *
 * The sentinel node belongs to the context being closed, and the worklet-ack
 * window holds a pending 5s flush that would otherwise fire against a dead
 * graph. The window is FLUSHED before it is dropped: those acks really
 * happened, and a teardown is no reason to lose them. Session totals survive,
 * because a route remount is not a new app session.
 */
export function disarmContextInstrumentation(): void {
	detachXrunSentinel();
	flushWorkletAckWindow();
	resetWorkletAckStats();
}

/**
 * P0: arm the non-running-state watchdog for this context.
 *
 * Lives here rather than in the engine for the same reason everything else in
 * this module does (convention D5): `audio-engine.svelte.ts` sits near the
 * ratchet's `file_size.max_frontend` cap and instrumentation is not playback.
 *
 * The `running` re-stamp that used to be the whole `statechange` listener is
 * now one branch of `stampContextDeviceFloors`, called on every transition, so
 * a context that starts suspended is still re-stamped the moment it runs.
 */
export function armAudioContextWatchdog(
	ctx: AudioContext,
	isAnyDeckPlaying: () => boolean
): void {
	ctx.addEventListener('statechange', () => {
		if (ctx.state === 'running') stampContextDeviceFloors(ctx);
	});
	installAudioContextWatchdog(
		ctx as unknown as WatchableAudioContext,
		{
			pushToast,
			recordPerfTiming: (kind, stages) => recordPerfTiming(kind, stages),
			sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms))
		},
		isAnyDeckPlaying
	);
}
