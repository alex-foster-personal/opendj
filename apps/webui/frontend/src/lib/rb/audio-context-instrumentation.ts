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

import { installAudioContextWatchdog } from '$lib/rb/audio-context-watchdog';
import { installOutputRebind, type OutputRebindHandle } from '$lib/rb/audio-output-rebind';
import { installOutputLiveness, type AudioOutputSnapshot } from '$lib/rb/audio-output-liveness';
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
import { attachMeterTaps, teardownMeterTaps, type MeterTapSource } from '$lib/rb/meter-tap';

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
/**
 * Arm the post-EQ channel level meters for this context.
 *
 * Fire-and-forget for the same reason as the sentinel: `addModule` is async and
 * the graph build is not, so the decks must not wait on an instrument. The
 * failure is RECORDED rather than swallowed - a tap that quietly failed to load
 * leaves every meter reading silence, which is indistinguishable from a paused
 * deck and is the worst possible failure for a level meter.
 */
export function armDeckMeters(
	ctx: AudioContext,
	sources: ReadonlyArray<MeterTapSource>
): void {
	void attachMeterTaps(ctx, sources).catch((error: unknown) => {
		recordPerfEvent(
			'deck-meters-failed',
			`channel level meters did not start, so every meter reads silence: ${String(error)}`
		);
	});
}

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
let _outputRebind: OutputRebindHandle | null = null;
let _outputLiveness: ReturnType<typeof installOutputLiveness> | null = null;

export function disarmContextInstrumentation(): void {
	detachXrunSentinel();
	// The meter taps and their zero-gain sink belong to the context being
	// closed, exactly like the sentinel above.
	teardownMeterTaps();
	flushWorkletAckWindow();
	resetWorkletAckStats();
	// Same reason as the sentinel above: this listener closes over the context
	// being closed. `noteOutputStall` fans out to EVERY registered listener, so
	// one left behind by a route unmount answers the next stall by resuming a
	// closed context, which rejects and raises an "output could not be
	// re-bound" error toast next to the live context's success one.
	_outputRebind?.uninstall();
	_outputRebind = null;
	// Same again for the liveness poll, which is a setInterval rather than a
	// listener: left behind it keeps waking every LIVENESS_POLL_MS forever, one
	// more timer per route visit. Its own guard keeps it quiet against a closed
	// context, so this is a leak rather than a wrong toast -- but nothing ever
	// stops it, and `_ensureGraph` arms a fresh one on the way back in.
	_outputLiveness?.uninstall();
	_outputLiveness = null;
	// The exported reader closes over the liveness handle above, so without this
	// it keeps answering after uninstall with whatever verdict was last frozen
	// (dead or ok) instead of reflecting that no graph is armed. Deleted rather
	// than left pointing at a synthetic idle snapshot: absent is exactly its
	// state before the first `armAudioContextWatchdog`, so disarm restores that
	// same pre-arm shape instead of inventing a new "idle with no context" one.
	if (typeof window !== 'undefined') {
		delete (window as Window & { __mdtAudioOutput?: () => AudioOutputSnapshot }).__mdtAudioOutput;
	}
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
	// No cast of any kind: WatchableAudioContext is declared narrowly enough that
	// a real AudioContext structurally satisfies it, which is the point of
	// declaring it that way rather than reaching for a double assertion.
	installAudioContextWatchdog(
		ctx,
		{
			pushToast,
			recordPerfTiming: (kind, stages) => recordPerfTiming(kind, stages),
			sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms))
		},
		isAnyDeckPlaying
	);
	// Chromium keeps a context `running` on a dead output stream after the device
	// changes under it (a phone call taking the headphones, Wed 2 Sep 2026), so
	// the statechange watchdog above never fires; this one cycles the output.
	// Held so `disarmContextInstrumentation` can uninstall it: the handle is the
	// only way to drop the module-level stall listener, and dropping the local
	// `ctx` reference is not enough to unregister it.
	_outputRebind?.uninstall();
	_outputRebind = installOutputRebind(
		ctx,
		{
			pushToast,
			recordPerfEvent: (kind, message) => recordPerfEvent(kind, message, null, 'info'),
			now: () => performance.now(),
			setTimeout: (fn, ms) => setTimeout(fn, ms),
			clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)
		},
		isAnyDeckPlaying,
		typeof navigator !== 'undefined' && navigator.mediaDevices ? navigator.mediaDevices : null
	);
	// A context can be `running`, advancing, and rendering into a dead device
	// (Wed 2 Sep 2026 18:33: no sound, every other signal green). The only
	// device-level tell the browser gives is outputLatency staying 0.
	_outputLiveness?.uninstall();
	_outputLiveness = installOutputLiveness(
		ctx,
		{
			pushToast,
			recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
			setInterval: (fn, ms) => setInterval(fn, ms),
			clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>)
		},
		isAnyDeckPlaying
	);
	// Agent parity: the same health an operator would read off the toasts.
	const liveness = _outputLiveness;
	(window as Window & { __mdtAudioOutput?: () => AudioOutputSnapshot }).__mdtAudioOutput = () =>
		liveness.snapshot();
}
