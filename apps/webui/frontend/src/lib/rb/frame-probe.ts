/**
 * Opt-in frame-time probe for comparing desktop shells (PERFPROBE-01).
 *
 * The packaged Tauri app on macOS runs in WKWebView, which exposes no remote
 * debugging port, so a frame rate measured from OUTSIDE the page is only
 * available in the Chromium shells. This probe measures from INSIDE the page,
 * the same way in every shell, and rides the existing client performance
 * sample (`client-performance-samples.ts`) to the engine, where
 * GET /api/v1/performance/telemetry/client-samples reads it back.
 *
 * Off unless asked for: `?perfprobe=1` on the page URL, or localStorage
 * `mdt.perfProbe` = `1`. When off, nothing here runs and the sample carries
 * `frame_probe: null`. When on, it costs one requestAnimationFrame callback
 * per frame writing one number into a preallocated ring: no allocation per
 * frame. The summary (sort + percentiles) runs once per sample window, on the
 * sampler's timer, never inside a frame.
 */

import { nativeShellKind } from '$lib/shell/native-shell';
import { readXrunSessionCounter } from './xrun-sentinel';

/** Frames kept per window. 2048 covers 34 s at 60 fps, 17 s at 120 fps. */
export const FRAME_PROBE_CAPACITY = 2048;
/** A frame longer than this is a visible stall (three 60 Hz frames). */
export const LONG_FRAME_MS = 50;
export {
	FRAME_PROBE_QUERY,
	FRAME_PROBE_STORAGE_KEY,
	frameProbeRequested
} from './frame-probe-flag';

export type ProbeShell = 'electron' | 'tauri' | 'browser';

export interface FrameProbeWindow {
	shell: ProbeShell;
	window_ms: number;
	frames: number;
	/** Frames kept for the percentiles; less than `frames` when the ring wrapped. */
	frames_sampled: number;
	fps: number;
	p50_ms: number | null;
	p95_ms: number | null;
	p99_ms: number | null;
	max_ms: number | null;
	long_frames: number;
	/** Audio-thread glitches the xrun sentinel counted in this window; null when it is not armed. */
	xruns: number | null;
}

export interface FrameProbe {
	/** Summarize the window since the last take and start a new one. */
	take(): FrameProbeWindow;
	stop(): void;
}

export function probeShell(scope: Parameters<typeof nativeShellKind>[0] = globalThis): ProbeShell {
	return nativeShellKind(scope) ?? 'browser';
}

function percentile(sorted: Float64Array, q: number): number {
	const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
	return sorted[index];
}

function round1(value: number): number {
	return Math.round(value * 10) / 10;
}

/**
 * Pure summary of one window. `intervals` holds frame durations in ms; only
 * the first `kept` entries are read. `frames` and `longFrames` are counted
 * over the whole window, including frames the ring overwrote.
 */
export function summarizeFrameWindow(input: {
	intervals: Float64Array;
	kept: number;
	frames: number;
	longFrames: number;
	windowMs: number;
	shell: ProbeShell;
	xruns: number | null;
}): FrameProbeWindow {
	const kept = Math.max(0, Math.min(input.kept, input.intervals.length));
	const sorted = input.intervals.slice(0, kept).sort();
	const windowMs = Math.max(0, input.windowMs);
	return {
		shell: input.shell,
		window_ms: Math.round(windowMs),
		frames: input.frames,
		frames_sampled: kept,
		fps: windowMs > 0 ? round1((input.frames * 1000) / windowMs) : 0,
		p50_ms: kept > 0 ? round1(percentile(sorted, 0.5)) : null,
		p95_ms: kept > 0 ? round1(percentile(sorted, 0.95)) : null,
		p99_ms: kept > 0 ? round1(percentile(sorted, 0.99)) : null,
		max_ms: kept > 0 ? round1(sorted[kept - 1]) : null,
		long_frames: input.longFrames,
		xruns: input.xruns
	};
}

function readXruns(): number | null {
	const counter = readXrunSessionCounter();
	// Zero reports means the sentinel never delivered a window, so a zero
	// count would be an unmeasured value dressed as a clean one.
	return counter.reports > 0 ? counter.xruns : null;
}

export function startFrameProbe(
	options: {
		raf?: (callback: (ts: number) => void) => number;
		cancelRaf?: (handle: number) => void;
		now?: () => number;
		shell?: ProbeShell;
		xruns?: () => number | null;
	} = {}
): FrameProbe {
	const raf = options.raf ?? ((callback) => requestAnimationFrame(callback));
	const cancelRaf = options.cancelRaf ?? ((handle) => cancelAnimationFrame(handle));
	const now = options.now ?? (() => performance.now());
	const shell = options.shell ?? probeShell();
	const xruns = options.xruns ?? readXruns;

	const ring = new Float64Array(FRAME_PROBE_CAPACITY);
	let write = 0;
	let frames = 0;
	let longFrames = 0;
	let lastTs: number | null = null;
	let windowStart = now();
	let xrunsAtStart = xruns();
	let handle: number | null = null;
	let stopped = false;

	const onFrame = (ts: number): void => {
		if (stopped) return;
		if (lastTs !== null) {
			const interval = ts - lastTs;
			ring[write] = interval;
			write = (write + 1) % FRAME_PROBE_CAPACITY;
			frames += 1;
			if (interval > LONG_FRAME_MS) longFrames += 1;
		}
		lastTs = ts;
		handle = raf(onFrame);
	};
	handle = raf(onFrame);

	return {
		take(): FrameProbeWindow {
			const takenAt = now();
			const kept = Math.min(frames, FRAME_PROBE_CAPACITY);
			const xrunsNow = xruns();
			const summary = summarizeFrameWindow({
				intervals: ring,
				kept,
				frames,
				longFrames,
				windowMs: takenAt - windowStart,
				shell,
				xruns: xrunsNow !== null && xrunsAtStart !== null ? xrunsNow - xrunsAtStart : null
			});
			write = 0;
			frames = 0;
			longFrames = 0;
			windowStart = takenAt;
			xrunsAtStart = xrunsNow;
			return summary;
		},
		stop(): void {
			stopped = true;
			if (handle !== null) cancelRaf(handle);
			handle = null;
		}
	};
}
