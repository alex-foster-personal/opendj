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
	XRUN_PARKED_GAP_MS,
	XRUN_REPORT_INTERVAL_MS,
	foldXrunReport,
	isXrunReport,
	quantumDurationMs,
	xrunGapThresholdMs,
	xrunReportMessage,
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

/** Cumulative xruns for this app session. Agent-readable, no UI required. */
export function readXrunSessionCounter(): XrunSessionCounter {
	return { ..._session };
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
	recordPerfEvent('xrun', xrunReportMessage(data));
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
	const thresholdMs = xrunGapThresholdMs(
		quantumDurationMs(RENDER_QUANTUM_FRAMES, ctx.sampleRate),
		ctx.baseLatency * 1000
	);
	const node = new AudioWorkletNode(ctx, XRUN_SENTINEL_PROCESSOR_NAME, {
		...XRUN_SENTINEL_NODE_OPTIONS,
		processorOptions: {
			thresholdMs,
			parkedGapMs: XRUN_PARKED_GAP_MS,
			reportIntervalMs: XRUN_REPORT_INTERVAL_MS
		}
	});
	node.port.onmessage = (event: MessageEvent) => _onReport(event.data);
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
		`glitch detection armed: gaps over ${Math.round(thresholdMs * 1000) / 1000}ms count as ` +
			`xruns, reported at most every ${XRUN_REPORT_INTERVAL_MS}ms`
	);
}

/** DevTools/agent helper: __mdtXruns() returns the session counter. */
export function installXrunSessionGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & { __mdtXruns?: () => XrunSessionCounter };
	w.__mdtXruns = () => readXrunSessionCounter();
}
