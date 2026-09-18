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
	noteRecoveryOpportunity
} from '$lib/rb/audio-context-watchdog';
import {
	AUDIO_OUTPUT_DEAD_TOAST,
	AudioContextIoTimeoutError,
	withAudioContextIoTimeout
} from '$lib/rb/audio-context-io-timeout';
import { installOutputRebind, type OutputRebindHandle } from '$lib/rb/audio-output-rebind';
import { installOutputLiveness, type AudioOutputSnapshot } from '$lib/rb/audio-output-liveness';
import {
	installOutputStallRecovery,
	type OutputStallRecoveryHandle
} from '$lib/rb/audio-output-stall-recovery';
import { clearAudioOutputHealth } from '$lib/rb/audio-output-health.svelte';
import { isMasterMuted } from '$lib/player/master-mute.svelte';
// Type-only: the probe and its browser wiring are loaded with a dynamic import
// in `armDeviceOutputProbe`, so they stay out of the first-paint "/" chunk.
import type { DeviceOutputProbeHandle } from '$lib/rb/device-output-probe';
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
import {
	attachMeterTaps,
	markMetersUnavailable,
	teardownMeterTaps,
	type MeterTapSource
} from '$lib/rb/meter-tap';
import {
	readPlayingPositions,
	recordUnexpectedPause,
	setPlayingPositionReader
} from '$lib/rb/unexpected-pause-report';

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
function reportAudioOutputDead(operation: string, timeoutMs: number): void {
	recordPerfEvent(
		'audio-output-dead',
		`AudioContext ${operation} timed out after ${timeoutMs}ms (watchdog)`,
		null,
		'error'
	);
	pushToast(AUDIO_OUTPUT_DEAD_TOAST, 'error');
}

/**
 * Resume a suspended or interrupted context with a bounded IO wait.
 *
 * On timeout: one `audio-output-dead` row, one error toast, then rethrow so play
 * rejects instead of hanging the page.
 */
export async function resumeAudioContextOrReportDead(
	ctx: { resume(): Promise<void> }
): Promise<void> {
	try {
		await withAudioContextIoTimeout('resume', ctx.resume());
	} catch (error: unknown) {
		if (error instanceof AudioContextIoTimeoutError) {
			reportAudioOutputDead(error.operation, error.timeoutMs);
		}
		throw error;
	}
}

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
 * Arm the post-trim, post-EQ, post-channel-fader level meters for this context.
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
		// Recorded AND surfaced: a perf-event row alone is a terminal error
		// masked as a silent reading (AGENTS.md L244-L246) - nobody watches the
		// ring live, so the meter itself has to say it is broken.
		//
		// Scoped to THIS context, though. Route cleanup calls `dispose()`
		// without awaiting it, so a rejection from a context torn down mid-arm
		// can land after a remount has armed a healthy new one; marking then
		// would leave a working meter reading "unavailable" for the rest of the
		// session. `markMetersUnavailable` drops a verdict from a context that
		// is no longer armed, and the row says which of the two happened so a
		// dropped verdict is still visible in the ring.
		const marked = markMetersUnavailable(ctx);
		recordPerfEvent(
			'deck-meters-failed',
			marked
				? `level meters did not start, so every meter is unavailable rather than reading silence: ${String(error)}`
				: `level meters failed for a context that is no longer armed, so the current graph's meters are left alone: ${String(error)}`
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
let _outputStallRecovery: OutputStallRecoveryHandle | null = null;
let _deviceOutputProbe: DeviceOutputProbeHandle | null = null;
/**
 * Bumped by every arm and disarm, so a probe whose dynamic import resolves
 * after its graph was disarmed or re-armed is never installed.
 */
let _deviceOutputProbeGeneration = 0;
let _browserLivenessSnapshot: AudioOutputSnapshot | null = null;
let _watchdogDetach: (() => void) | null = null;
let _recoveryEdgesDetach: (() => void) | null = null;

/**
 * The DOM edges that mean "ask the watchdog to try again".
 *
 * The context watchdog's resume schedule is bounded on purpose and spans about
 * nine seconds. Everything that actually takes an output device away lasts
 * longer than that -- a Bluetooth re-pair, a phone call, a screen lock, this
 * app's own packaged WKWebView sitting behind another window -- and
 * `statechange` will not fire again, because a context that stayed
 * `interrupted` never changed state. These two edges are the app's only news
 * that the world moved:
 *
 *   - `visibilitychange` to visible: the operator came back to the window, which
 *     on macOS is also when a backgrounded WKWebView is allowed to hold an
 *     output stream again.
 *   - `devicechange`: the device list moved, so the device may be back. The
 *     rebind module listens to this too, for the DIFFERENT failure where the
 *     context stays `running` on a dead stream; that path cannot help here,
 *     because its cycle begins by checking for `running`.
 *
 * Installed once per armed graph and dropped on disarm, so a route remount does
 * not stack a second pair.
 */
function installRecoveryOpportunities(): void {
	_recoveryEdgesDetach?.();
	if (typeof document === 'undefined' && typeof navigator === 'undefined') return;
	const onVisibility = (): void => {
		if (document.visibilityState === 'visible') noteRecoveryOpportunity('the window became visible');
	};
	const onDeviceChange = (): void => noteRecoveryOpportunity('the output device list changed');
	const media = typeof navigator !== 'undefined' ? navigator.mediaDevices : undefined;
	if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);
	media?.addEventListener('devicechange', onDeviceChange);
	_recoveryEdgesDetach = () => {
		if (typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', onVisibility);
		}
		media?.removeEventListener('devicechange', onDeviceChange);
	};
}

/**
 * Issue #923: install the OS output-device probe for the armed graph.
 *
 * Loaded with a dynamic import so the probe leaves the first-paint "/" chunk
 * (library bundle budget). Install therefore lands once that chunk has loaded
 * rather than synchronously with the graph arm. Nothing depends on it being
 * there sooner: every reader goes through `_deviceOutputProbe?.` or the
 * control module's `_handle?.`, and the switch-output action is only offered
 * once a device snapshot with `probe_available` exists, which only an
 * installed probe can publish. `install` in `device-output-probe-browser.ts` registers the
 * handle and republishes the latest snapshot at once, and the probe's own
 * dead-verdict edge then catches a dead reading that arrived mid-import.
 */
function armDeviceOutputProbe(isAnyDeckPlaying: () => boolean): void {
	const generation = dropDeviceOutputProbe();
	// `probe.install`, not a destructured binding: Vite rewrites a destructured
	// dynamic import into a wrapper that costs first-paint bytes.
	void import('$lib/rb/device-output-probe-browser').then(
		(probe) =>
			// Disarmed or re-armed while the import was in flight: that graph is gone.
			generation === _deviceOutputProbeGeneration &&
			(_deviceOutputProbe = probe.install(
				() => _browserLivenessSnapshot,
				isAnyDeckPlaying,
				isMasterMuted,
				pushToast
			)),
		(error: unknown) =>
			// Recorded, never swallowed: a probe that silently failed to load
			// would leave the device bar reading "no data" forever.
			recordPerfEvent('device-output-probe-failed', String(error), null, 'error')
	);
}

/**
 * Uninstall the current probe (which also unregisters it from the Switch
 * output control) and supersede any arm still in flight.
 */
function dropDeviceOutputProbe(): number {
	_deviceOutputProbe?.uninstall();
	_deviceOutputProbe = null;
	return ++_deviceOutputProbeGeneration;
}

export function disarmContextInstrumentation(): void {
	setPlayingPositionReader(null);
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
	_outputStallRecovery = null;
	dropDeviceOutputProbe();
	_browserLivenessSnapshot = null;
	// Same identity-scoped teardown as the two above. The watchdog's recovery
	// listener sits on a module-level fan-out, so one left behind by a route
	// unmount answers every later device change by resuming a CLOSED context.
	_watchdogDetach?.();
	_watchdogDetach = null;
	_recoveryEdgesDetach?.();
	_recoveryEdgesDetach = null;
	// The bar under master volume must go back to "no data" rather than keep
	// quoting a device snapshot from the context just closed.
	clearAudioOutputHealth();
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
	isAnyDeckPlaying: () => boolean,
	recreateGraph: (() => Promise<void>) | null = null
): void {
	ctx.addEventListener('statechange', () => {
		if (ctx.state === 'running') stampContextDeviceFloors(ctx);
	});
	// No cast of any kind: WatchableAudioContext is declared narrowly enough that
	// a real AudioContext structurally satisfies it, which is the point of
	// declaring it that way rather than reaching for a double assertion.
	//
	// Held now, where it used to be discarded. The watchdog registers a
	// module-level recovery listener, and a discarded detach leaks one of those
	// per route visit -- each still holding a closed context to resume.
	_watchdogDetach?.();
	_watchdogDetach = installAudioContextWatchdog(
		ctx,
		{
			pushToast,
			recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
			recordPerfTiming: (kind, stages) => recordPerfTiming(kind, stages),
			sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
			noteUnexpectedPause: (state) => {
				const positions = readPlayingPositions();
				const first = positions[0];
				if (first === undefined) return;
				recordUnexpectedPause({
					cause: 'context-suspended',
					deck: first.deck,
					position_ms: first.position_ms,
					context_state: state,
					decoded_duration_ms: first.decoded_duration_ms ?? null,
					metadata_duration_ms: first.metadata_duration_ms ?? null
				});
			}
		},
		isAnyDeckPlaying
	);
	installRecoveryOpportunities();
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
			// Severity comes FROM the rebind, it is not pinned here: a failed
			// re-bind records at `error` so `recordPerfEvent` escalates it to the
			// engine's client-error log. Pinning every row at `info` is why the
			// Wed 9 Sep 2026 17:44Z cutout left no server-side trace.
			recordPerfEvent: (kind, message, severity = 'info') =>
				recordPerfEvent(kind, message, null, severity),
			now: () => performance.now(),
			setTimeout: (fn, ms) => setTimeout(fn, ms),
			clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)
		},
		isAnyDeckPlaying,
		typeof navigator !== 'undefined' && navigator.mediaDevices ? navigator.mediaDevices : null
	);
	_outputStallRecovery = installOutputStallRecovery(_outputRebind, {
		recreateGraph,
		pushToast,
		recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
		now: () => performance.now()
	});
	// A context can be `running`, advancing, and rendering into a dead device
	// (Wed 2 Sep 2026 18:33: no sound, every other signal green). The only
	// device-level tell the browser gives is outputLatency staying 0.
	armDeviceOutputProbe(isAnyDeckPlaying);
	_outputLiveness?.uninstall();
	_outputLiveness = installOutputLiveness(
		ctx,
		{
			pushToast,
			recordPerfEvent: (kind, message, severity) => recordPerfEvent(kind, message, null, severity),
			setInterval: (fn, ms) => setInterval(fn, ms),
			clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
			now: () => performance.now(),
			recoverOutput: () => {
				void _outputStallRecovery?.recover();
			},
			onSnapshot: (snapshot) => {
				// The probe folds this into the bar and owns the dead-verdict edge.
				_browserLivenessSnapshot = snapshot;
				_deviceOutputProbe?.republish();
			}
		},
		isAnyDeckPlaying
	);
	// Agent parity: the same health an operator would read off the toasts.
	const liveness = _outputLiveness;
	(window as Window & { __mdtAudioOutput?: () => AudioOutputSnapshot }).__mdtAudioOutput = () =>
		liveness.snapshot();
}
