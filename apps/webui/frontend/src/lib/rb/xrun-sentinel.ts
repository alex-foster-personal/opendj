/**
 * S1 / Q2: the main-thread half of the audio-thread glitch detector.
 *
 * Owns the worklet module load, the sentinel node, and the session counter the
 * reports fold into. The arithmetic lives in `$lib/rb/xrun-math`; the callback
 * loop lives in `xrun-sentinel-processor.js`. This file is only the wiring.
 *
 * WHY A DEDICATED PROCESSOR rather than a counter inside the stretch worklet:
 * the Signalsmith worklet is vendored third-party code and stays untouched, and
 * a detector must not share a callback with the thing it is measuring. This one
 * writes no audio at all, so a bug in it cannot become a sound.
 *
 * WHY IT IS ALWAYS ON: an xrun that happened before somebody thought to enable
 * a counter is an xrun nobody can diagnose. The cost is one silent processor
 * callback per render quantum and, on a healthy machine, zero MessagePort
 * traffic and zero ring rows, because the processor only posts a window that
 * had something in it.
 *
 * MEASURED end to end against the built asset in a real browser, Sun 31 Aug
 * 2026 (44100Hz, baseLatency 5.805ms, threshold 9.707ms):
 *   idle machine   -> 629 callbacks in 2006ms, ZERO reports posted
 *   starved thread -> 80 xruns in 80 callbacks over 2000ms, worst gap 25ms,
 *                     against a deliberately CPU-burning worklet in the graph
 * So it is silent when healthy and it does detect a real audio-thread stall,
 * rather than merely running without complaint.
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import {
	EMPTY_XRUN_SESSION,
	RENDER_QUANTUM_FRAMES,
	XRUN_CADENCE_QUANTILE,
	XRUN_CADENCE_WARMUP_CALLBACKS,
	XRUN_CADENCE_WINDOW,
	XRUN_GAP_FACTOR,
	XRUN_GAP_FLOOR_MS,
	XRUN_PARKED_GAP_MS,
	XRUN_REPORT_INTERVAL_MS,
	foldXrunReport,
	isXrunReport,
	quantumDurationMs,
	xrunReportMessage,
	xrunThresholdFromCadenceMs,
	type XrunSessionCounter
} from '$lib/rb/xrun-math';
import xrunSentinelModuleUrl from '$lib/rb/xrun-sentinel-processor.js?url';

export const XRUN_SENTINEL_PROCESSOR_NAME = 'mdt-xrun-sentinel';

/** Silent by construction, and silenced again by a zero gain on the way out. */
const XRUN_SENTINEL_NODE_OPTIONS: Readonly<AudioWorkletNodeOptions> = Object.freeze({
	numberOfInputs: 0,
	numberOfOutputs: 1,
	outputChannelCount: [1]
});

let _session: XrunSessionCounter = { ...EMPTY_XRUN_SESSION };
let _node: AudioWorkletNode | null = null;
let _flushSequence = 0;
const _flushWaiters = new Map<
	string,
	{ resolve: (counter: XrunSessionCounter) => void; reject: (error: Error) => void; timeout: number }
>();

/** Cumulative xruns for this app session. Agent-readable, no UI required. */
export function readXrunSessionCounter(): XrunSessionCounter {
	return { ..._session };
}

/** Flush the audio-thread xrun window and resolve only after its port acknowledgement. */
export function flushXrunSessionCounter(): Promise<XrunSessionCounter> {
	if (_node === null) {
		return Promise.reject(new Error('xrun sentinel is not armed, so its counter cannot be flushed'));
	}
	const requestId = `xrun-flush-${++_flushSequence}`;
	return new Promise((resolve, reject) => {
		const timeout = window.setTimeout(() => {
			_flushWaiters.delete(requestId);
			reject(new Error('xrun sentinel did not acknowledge the flush within 5 seconds'));
		}, 5_000);
		_flushWaiters.set(requestId, { resolve, reject, timeout });
		_node?.port.postMessage({ kind: 'xrun-flush', requestId });
	});
}

function _rejectFlushWaiters(reason: string): void {
	for (const waiter of _flushWaiters.values()) {
		window.clearTimeout(waiter.timeout);
		waiter.reject(new Error(reason));
	}
	_flushWaiters.clear();
}

/**
 * Detach the sentinel from a context that is going away.
 *
 * The session totals deliberately SURVIVE: they are cumulative for the app
 * session, and a route remount that rebuilt the graph is not a new session. A
 * counter that silently restarted at zero on every navigation would report a
 * healthy machine to anyone who navigated once.
 */
export function detachXrunSentinel(): void {
	if (_node === null) return;
	_rejectFlushWaiters('xrun sentinel detached before it acknowledged the flush');
	_node.port.onmessage = null;
	_node.disconnect();
	_node = null;
}

function _onReport(data: unknown): void {
	if (!isXrunReport(data)) {
		recordPerfEvent(
			'xrun-sentinel-bad-report',
			`xrun sentinel posted a message this build cannot read: ${JSON.stringify(data)}`
		);
		return;
	}
	_session = foldXrunReport(_session, data);
	// error severity, so perf-event-log escalates it to /api/v1/client-errors
	// (rate-limited there to one POST per kind per minute). A window the audio
	// thread reports as late is an audio-liveness failure, not a notice.
	recordPerfEvent('xrun', xrunReportMessage(data), null, 'error');
}

function _onMessage(data: unknown): void {
	if (
		typeof data === 'object' && data !== null &&
		(data as { kind?: unknown }).kind === 'xrun-flush-ack' &&
		typeof (data as { requestId?: unknown }).requestId === 'string'
	) {
		const requestId = (data as { requestId: string }).requestId;
		const waiter = _flushWaiters.get(requestId);
		if (waiter === undefined) {
			recordPerfEvent('xrun-sentinel-bad-report', `unexpected xrun flush acknowledgement: ${requestId}`);
			return;
		}
		window.clearTimeout(waiter.timeout);
		_flushWaiters.delete(requestId);
		if ((data as { judging?: unknown }).judging !== true) {
			waiter.reject(new Error('xrun sentinel has not completed cadence warmup; its counter is not ready'));
			return;
		}
		waiter.resolve(readXrunSessionCounter());
		return;
	}
	_onReport(data);
}

/**
 * Load the sentinel module into `ctx` and connect it through a zero gain.
 *
 * Rejects rather than reporting its own failure, so the single caller owns the
 * one catch and the failure is recorded in exactly one place.
 */
export async function installXrunSentinel(ctx: AudioContext): Promise<void> {
	if (ctx.audioWorklet === undefined) {
		throw new Error('AudioWorklet is unavailable; the xrun sentinel cannot start');
	}
	await ctx.audioWorklet.addModule(xrunSentinelModuleUrl);
	// The best cadence estimate available on THIS side of the port: one period,
	// from baseLatency, floored at the render quantum because baseLatency reads 0
	// before a device is attached. Derived through the same function the worklet's
	// own measurement uses, so the seed and the measurement cannot drift apart in
	// how they turn a period into a threshold - only in the period itself, which
	// is the whole point of measuring it over there.
	const thresholdMs = xrunThresholdFromCadenceMs([
		Math.max(ctx.baseLatency * 1000, quantumDurationMs(RENDER_QUANTUM_FRAMES, ctx.sampleRate))
	]);
	const node = new AudioWorkletNode(ctx, XRUN_SENTINEL_PROCESSOR_NAME, {
		...XRUN_SENTINEL_NODE_OPTIONS,
		processorOptions: {
			// A SEED ONLY. No gap is ever classified against it: the processor
			// measures the device's real callback period and replaces this before
			// it judges anything. It is derived from baseLatency, which is the
			// number that was wrong on Wed 2 Sep 2026, and it is kept solely so a
			// report that somehow escapes before warmup carries a finite figure.
			thresholdMs,
			parkedGapMs: XRUN_PARKED_GAP_MS,
			reportIntervalMs: XRUN_REPORT_INTERVAL_MS,
			// Policy still lives on this side: the worklet measures, it does not
			// decide what a healthy multiple of the measured period is.
			cadenceQuantile: XRUN_CADENCE_QUANTILE,
			cadenceWindow: XRUN_CADENCE_WINDOW,
			warmupCallbacks: XRUN_CADENCE_WARMUP_CALLBACKS,
			gapFactor: XRUN_GAP_FACTOR,
			gapFloorMs: XRUN_GAP_FLOOR_MS
		}
	});
	node.port.onmessage = (event: MessageEvent) => _onMessage(event.data);
	node.addEventListener('processorerror', () => {
		recordPerfEvent(
			'xrun-sentinel-failed',
			'the xrun sentinel processor threw during rendering; glitch counting is now blind ' +
				'for the rest of this audio context'
		);
	});
	// Zero gain: the sentinel is scheduled by being part of a live chain to the
	// destination, and nothing it could ever emit can reach the output.
	const mute = ctx.createGain();
	mute.gain.value = 0;
	node.connect(mute);
	mute.connect(ctx.destination);
	_node = node;
	recordPerfEvent(
		'xrun-sentinel-armed',
		`glitch detection armed: measuring the device's own callback cadence over ` +
			`${XRUN_CADENCE_WARMUP_CALLBACKS} callbacks before judging any of them (seed ` +
			`threshold ${Math.round(thresholdMs * 1000) / 1000}ms from baseLatency, never ` +
			`judged against), reported at most every ${XRUN_REPORT_INTERVAL_MS}ms`
	);
}

/** DevTools/agent helpers expose both counter reads and acknowledged flushes. */
export function installXrunSessionGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & {
		__mdtFlushXruns?: () => Promise<XrunSessionCounter>;
		__mdtXruns?: () => XrunSessionCounter;
	};
	w.__mdtXruns = () => readXrunSessionCounter();
	w.__mdtFlushXruns = () => flushXrunSessionCounter();
}
