/**
 * The machine's pressure, cached OFF the load path.
 *
 * WHY THIS EXISTS. A deck-load row that says "1.4 s" without saying what the
 * machine was doing is not a measurement, it is an anecdote. The register
 * (docs/perf/performance-register.md) carries a beatgrid lane measured at
 * "load average 554 and roughly 71 MB free RAM" whose whole runtime budget is
 * documented as untested for exactly that reason, and a waveform decode that
 * moved 0.73 s -> 7.53 s in one evening on nothing but the machine's load.
 *
 * WHY IT IS A POLLER AND NOT A READ. The load path may not grow an await, a
 * network call, or a round trip to the engine: the whole point of the program
 * is the time between a gesture and audible output, and instrumenting it by
 * lengthening it would be self-defeating. So a low-rate poller keeps the last
 * reading in memory and a load stamps the CACHED snapshot plus its age. The
 * age is not decoration -- a 40-second-old reading is a different piece of
 * evidence from a fresh one, and the reader gets to decide.
 *
 * NEVER A ZERO. If no snapshot has ever arrived, a row carries
 * `pressure=unknown` and no numeric fields at all. `load_avg_1m=0` on a box
 * that was never sampled is indistinguishable from a genuinely idle box, and
 * an unmeasured condition that renders as a real reading is the exact defect
 * .claude/rules/verification.md exists to forbid. The same rule applies
 * field by field: the engine may be able to read the load average and not the
 * free page count, and the fields it could not read are ABSENT, never zero.
 */

import { API_BASE } from '$lib/api/client';

/** How often the engine is asked, while the page is visible. Low-rate on
 * purpose: this is a condition that moves over seconds, and the fetch shares
 * the origin's connection budget with audio. */
export const PRESSURE_POLL_INTERVAL_MS = 10_000;

const PRESSURE_PATH = '/api/v1/performance/telemetry/pressure';

/**
 * One reading, as this client received it.
 *
 * Every numeric field is nullable and every null means NOT MEASURED. There is
 * no default anywhere in this module.
 */
export interface PressureSnapshot {
	/** 1-minute load average, or null if the engine could not read it. */
	readonly loadAvg1m: number | null;
	/** Free physical memory in MB, or null. */
	readonly memFreeMb: number | null;
	/** Swap in use in MB, or null. */
	readonly swapUsedMb: number | null;
	/** `Date.now()` when this client received it. */
	readonly receivedAtMs: number;
	/** How stale the reading already was server-side when it was handed over,
	 * so `pressure_age_ms` counts from the SAMPLE, not from the response. */
	readonly serverCacheAgeMs: number;
}

let _snapshot: PressureSnapshot | null = null;

/** The last reading, or null if none has ever arrived. */
export function readMachinePressure(): PressureSnapshot | null {
	return _snapshot;
}

/** Overwrite the cache. Exported so the poller and its tests share one door
 * rather than a test reaching into module state. */
export function storeMachinePressure(snapshot: PressureSnapshot): void {
	_snapshot = snapshot;
}

/** The engine's answer. Absent fields stay absent; this module never fills
 * one in. */
interface PressureResponse {
	available?: unknown;
	load_avg_1m?: unknown;
	mem_free_mb?: unknown;
	swap_used_mb?: unknown;
	cache_age_ms?: unknown;
}

function _finiteOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Turn one engine response into a snapshot, or null if the engine said it
 * could not measure.
 *
 * `available !== true` is rejected rather than trusted-if-truthy: the engine
 * returns `{available: false, reason: ...}` when the native probe is not
 * importable (the packaged app ships `apps/` and not `scripts/`), and a
 * response that says it holds no reading must never become a row that claims
 * one.
 */
export function pressureSnapshotFrom(
	body: PressureResponse,
	receivedAtMs: number
): PressureSnapshot | null {
	if (body.available !== true) return null;
	const loadAvg1m = _finiteOrNull(body.load_avg_1m);
	const memFreeMb = _finiteOrNull(body.mem_free_mb);
	const swapUsedMb = _finiteOrNull(body.swap_used_mb);
	// A response carrying no readable number at all is the same as no response:
	// keeping it would let a row print `pressure_age_ms` beside nothing.
	if (loadAvg1m === null && memFreeMb === null && swapUsedMb === null) return null;
	return {
		loadAvg1m,
		memFreeMb,
		swapUsedMb,
		receivedAtMs,
		serverCacheAgeMs: _finiteOrNull(body.cache_age_ms) ?? 0
	};
}

/**
 * The labels a deck-load row stamps, given the cache and the instant.
 *
 * Pure, and separated from the poller for the same reason the interval math
 * is separated from the registry: the `unknown` branch has to be as easy to
 * exercise as the populated one.
 */
export function pressureLabels(
	snapshot: PressureSnapshot | null,
	nowMs: number
): Record<string, string> {
	if (snapshot === null) return { pressure: 'unknown' };
	const labels: Record<string, string> = {
		// Counts from the SAMPLE the engine took, not from the response this
		// client received, so a slow round trip cannot make a stale reading
		// look fresh.
		pressure_age_ms: String(
			Math.max(0, Math.round(nowMs - snapshot.receivedAtMs + snapshot.serverCacheAgeMs))
		)
	};
	if (snapshot.loadAvg1m !== null) labels.load_avg_1m = String(snapshot.loadAvg1m);
	if (snapshot.memFreeMb !== null) labels.mem_free_mb = String(snapshot.memFreeMb);
	if (snapshot.swapUsedMb !== null) labels.swap_used_mb = String(snapshot.swapUsedMb);
	return labels;
}

/** One poll. Never throws: an engine that is down is a condition this client
 * reports as unknown, not an exception thrown into a timer. */
async function _pollOnce(): Promise<void> {
	try {
		const response = await fetch(`${API_BASE}${PRESSURE_PATH}`, {
			headers: { accept: 'application/json' }
		});
		if (!response.ok) return;
		const body = (await response.json()) as PressureResponse;
		const snapshot = pressureSnapshotFrom(body, Date.now());
		if (snapshot !== null) storeMachinePressure(snapshot);
	} catch {
		// The previous snapshot stands, and its age keeps growing, which is the
		// honest reading: nothing new arrived. Clearing it here would throw away
		// evidence a reader can still weigh for themselves.
	}
}

/**
 * Start polling. Returns the teardown, which the caller owns.
 *
 * The timer runs only while the page is VISIBLE. A hidden tab is a tab nobody
 * is loading decks in, so its readings are both useless and, on a laptop, a
 * wake-up the machine did not need -- and the browser throttles the timer to
 * roughly one minute anyway, which would quietly turn a documented 10 s
 * cadence into an undocumented one. Stopping outright and resampling on the
 * way back is the honest version of what the browser would do to us.
 */
export function startMachinePressurePolling(): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	let timer: ReturnType<typeof setInterval> | null = null;

	const stopTimer = (): void => {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	};

	const startTimer = (): void => {
		if (timer !== null) return;
		timer = setInterval(() => void _pollOnce(), PRESSURE_POLL_INTERVAL_MS);
	};

	const onVisibilityChange = (): void => {
		if (document.visibilityState === 'visible') {
			// Resample immediately: the snapshot is as old as the hidden stretch
			// was long, and the first load after a tab comes back is exactly the
			// one somebody is watching.
			void _pollOnce();
			startTimer();
		} else {
			stopTimer();
		}
	};

	document.addEventListener('visibilitychange', onVisibilityChange);
	if (document.visibilityState === 'visible') {
		void _pollOnce();
		startTimer();
	}

	return () => {
		stopTimer();
		document.removeEventListener('visibilitychange', onVisibilityChange);
	};
}
