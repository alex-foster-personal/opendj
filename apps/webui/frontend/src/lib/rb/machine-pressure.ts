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

import { API_BASE } from '$lib/api/base';
import { bootScheduler, type BootScheduler } from './boot-scheduler';

/** How often the engine is asked, while the page is visible. Low-rate on
 * purpose: this is a condition that moves over seconds, and the fetch shares
 * the origin's connection budget with audio. */
export const PRESSURE_POLL_INTERVAL_MS = 10_000;

/** Locked PERFMODE-04 shed thresholds (issue #1984). */
export const KERNEL_ELEVATED_LEVEL = 2;
export const PRESSURE_CHURN_WEIGHT_SWAP = 10;
export const PRESSURE_CHURN_EARLY_WARNING = 500;
export const S1_XRUN_DELTA_MAX = 0;

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
	/** Kernel level for shed thresholds, or null if absent or unreadable. */
	readonly kernelLevel: number | null;
	/** Kernel memory pressure level (1, 2, or 4), or null when unreadable. */
	readonly kernelMemoryPressureLevel: 1 | 2 | 4 | null;
	/** Churn score (swap_rate * weight + decomp_rate), or null if absent. */
	readonly churnScore: number | null;
	/** Swap rate samples per second, or null if absent. */
	readonly swapRate: number | null;
	/** Decompression rate samples per second, or null if absent. */
	readonly decompRate: number | null;
	/** Activity Monitor band when kernel level was read, or null. */
	readonly band: 'fine' | 'warning' | 'critical' | null;
	/** Server sampler interval governing the next sample, or null. */
	readonly sampleIntervalMs: number | null;
	/** Compressor footprint in MB, or null when unreadable. */
	readonly compressedMb: number | null;
	/**
	 * `Date.now()` when this client ISSUED the request, not when the body
	 * finished parsing.
	 *
	 * Anchoring to the request is what keeps the age from flattering itself:
	 * a delayed response would otherwise have its round trip and event-loop
	 * wait erased, and an old sample would be labelled fresh. Anchored here
	 * the age can only ever OVERSTATE how stale a reading is, which is the
	 * safe direction for a number a reader uses to decide whether to trust
	 * the row beside it (#1658 review).
	 */
	readonly requestedAtMs: number;
	/** How stale the reading already was server-side when it was handed over,
	 * so `pressure_age_ms` counts from the SAMPLE, not from the response. */
	readonly serverCacheAgeMs: number;
}

let _snapshot: PressureSnapshot | null = null;
const _listeners: Array<(snapshot: PressureSnapshot | null) => void> = [];

/** Subscribe to accepted pressure stores. Fires only when a newer snapshot lands. */
export function subscribeMachinePressure(
	listener: (snapshot: PressureSnapshot | null) => void
): () => void {
	_listeners.push(listener);
	return () => {
		const index = _listeners.indexOf(listener);
		if (index >= 0) _listeners.splice(index, 1);
	};
}

/** The last reading, or null if none has ever arrived. */
export function readMachinePressure(): PressureSnapshot | null {
	return _snapshot;
}

/**
 * Store a reading, unless an EQUAL OR NEWER one is already held.
 *
 * Polls can overlap: the 10s timer does not wait for the previous request,
 * and a slow one completing after a fast one would otherwise overwrite the
 * newer reading with the older, so the cache would go BACKWARDS in time
 * while every field still looked valid (#1658 review). Ordering is decided
 * on the request instant, since that is when each sample was asked for.
 *
 * Returns whether the store happened, so a caller (and a test) can tell a
 * rejected late arrival from an accepted one.
 */
export function storeMachinePressure(snapshot: PressureSnapshot): boolean {
	if (_snapshot !== null && snapshot.requestedAtMs <= _snapshot.requestedAtMs) return false;
	_snapshot = snapshot;
	for (const listener of _listeners) listener(snapshot);
	return true;
}

/** True when the KERNEL reports memory pressure (level 2 or above). The churn
 * early warning alone does not count: it leads the kernel (PERFMODE-18). */
export function kernelPressureIsElevated(snapshot: PressureSnapshot | null): boolean {
	return snapshot !== null && snapshot.kernelLevel !== null && snapshot.kernelLevel >= KERNEL_ELEVATED_LEVEL;
}

/** True when kernel or churn crosses the locked PERFMODE-04 thresholds. */
export function pressureIsElevated(snapshot: PressureSnapshot | null): boolean {
	if (snapshot === null) return false;
	if (kernelPressureIsElevated(snapshot)) return true;
	if (snapshot.churnScore !== null && snapshot.churnScore >= PRESSURE_CHURN_EARLY_WARNING)
		return true;
	return false;
}

/** The engine's answer. Absent fields stay absent; this module never fills
 * one in. */
interface PressureResponse {
	available?: unknown;
	load_avg_1m?: unknown;
	mem_free_mb?: unknown;
	swap_used_mb?: unknown;
	cache_age_ms?: unknown;
	kernel_level?: unknown;
	kernel_memory_pressure_level?: unknown;
	churn_score?: unknown;
	swap_rate?: unknown;
	decomp_rate?: unknown;
	band?: unknown;
	sample_interval_ms?: unknown;
	compressed_mb?: unknown;
}

function _finiteOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function _kernelLevelOrNull(value: unknown): 1 | 2 | 4 | null {
	return value === 1 || value === 2 || value === 4 ? value : null;
}

function _bandOrNull(value: unknown): 'fine' | 'warning' | 'critical' | null {
	return value === 'fine' || value === 'warning' || value === 'critical' ? value : null;
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
	requestedAtMs: number
): PressureSnapshot | null {
	if (body.available !== true) return null;
	const loadAvg1m = _finiteOrNull(body.load_avg_1m);
	const memFreeMb = _finiteOrNull(body.mem_free_mb);
	const swapUsedMb = _finiteOrNull(body.swap_used_mb);
	const kernelMemoryPressureLevel = _kernelLevelOrNull(body.kernel_memory_pressure_level);
	const swapRate = _finiteOrNull(body.swap_rate);
	const decompRate = _finiteOrNull(body.decomp_rate);
	const churnFromBody = _finiteOrNull(body.churn_score);
	const churnScore =
		churnFromBody ??
		(swapRate !== null && decompRate !== null
			? swapRate * PRESSURE_CHURN_WEIGHT_SWAP + decompRate
			: null);
	const band = _bandOrNull(body.band);
	const sampleIntervalMs = _finiteOrNull(body.sample_interval_ms);
	const compressedMb = _finiteOrNull(body.compressed_mb);
	const kernelLevel = _finiteOrNull(body.kernel_level) ?? kernelMemoryPressureLevel;
	// A response carrying no readable number at all is the same as no response:
	// keeping it would let a row print `pressure_age_ms` beside nothing.
	if (
		loadAvg1m === null &&
		memFreeMb === null &&
		swapUsedMb === null &&
		kernelLevel === null &&
		kernelMemoryPressureLevel === null &&
		churnScore === null &&
		compressedMb === null
	)
		return null;
	return {
		loadAvg1m,
		memFreeMb,
		swapUsedMb,
		kernelLevel,
		kernelMemoryPressureLevel,
		churnScore,
		swapRate,
		decompRate,
		band,
		sampleIntervalMs,
		compressedMb,
		requestedAtMs,
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
			Math.max(0, Math.round(nowMs - snapshot.requestedAtMs + snapshot.serverCacheAgeMs))
		)
	};
	if (snapshot.loadAvg1m !== null) labels.load_avg_1m = String(snapshot.loadAvg1m);
	if (snapshot.memFreeMb !== null) labels.mem_free_mb = String(snapshot.memFreeMb);
	if (snapshot.swapUsedMb !== null) labels.swap_used_mb = String(snapshot.swapUsedMb);
	if (snapshot.kernelLevel !== null) labels.kernel_level = String(snapshot.kernelLevel);
	if (snapshot.kernelMemoryPressureLevel !== null) {
		labels.kernel_memory_pressure_level = String(snapshot.kernelMemoryPressureLevel);
	}
	if (snapshot.churnScore !== null) labels.churn_score = String(snapshot.churnScore);
	return labels;
}

/** One poll. Never throws: an engine that is down is a condition this client
 * reports as unknown, not an exception thrown into a timer. */
async function _pollOnce(): Promise<void> {
	// Stamped BEFORE the request, so the age carries the round trip instead of
	// discarding it, and so two overlapping polls can be ordered by when each
	// was asked for rather than by which happened to finish first.
	const requestedAtMs = Date.now();
	try {
		const response = await fetch(`${API_BASE}${PRESSURE_PATH}`, {
			headers: { accept: 'application/json' }
		});
		if (!response.ok) return;
		const body = (await response.json()) as PressureResponse;
		const snapshot = pressureSnapshotFrom(body, requestedAtMs);
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
 *
 * The FIRST poll is deferred out of the boot request burst (PERF-R6), same
 * convention as usage-heartbeat's first check-in: fired at mount it would
 * compete with a boot-time deck load's four fetches for the six-connection
 * origin and invoke the sysctl/vm_stat sampler on the exact path the
 * scheduler exists to keep quiet (#1658 review). That deferral has exactly
 * one entrance regardless of which event triggers it (mount visible, or a
 * page that starts hidden and is later shown): a page that opens in a
 * background tab and is switched to before the boot window closes owns the
 * same window as one that opened in the foreground, so the visibility
 * handler queues through the scheduler too until that first poll has
 * actually released, not just been requested -- otherwise a hide/show while
 * still queued would fire it a second time (#1658 review). Once released,
 * the interval and the visibility listener behave exactly as before: a
 * hidden tab stops the timer, a visible one resamples immediately.
 */
export function startMachinePressurePolling(
	scheduler: BootScheduler = bootScheduler
): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	let timer: ReturnType<typeof setInterval> | null = null;
	let firstPollScheduled = false;
	let firstPollReleased = false;

	const stopTimer = (): void => {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	};

	const startTimer = (): void => {
		if (timer !== null) return;
		timer = setInterval(() => void _pollOnce(), PRESSURE_POLL_INTERVAL_MS);
	};

	const runFirstPoll = (): void => {
		// Set unconditionally: the boot window has closed either way, so a later
		// visibility change takes the immediate post-boot path below rather than
		// queuing a second deferral. Whether THIS release actually polls depends
		// on the page being visible right now -- the scheduler's clock is not the
		// page's, so by the time it fires the tab may have gone hidden again
		// (#1658 review).
		firstPollReleased = true;
		if (document.visibilityState !== 'visible') return;
		void _pollOnce();
		startTimer();
	};

	const onVisibilityChange = (): void => {
		if (document.visibilityState === 'visible') {
			if (firstPollReleased) {
				// The boot window has actually closed (not just "probably has"):
				// resample immediately, same as any later hide/show.
				void _pollOnce();
				startTimer();
			} else if (!firstPollScheduled) {
				// Either this page's first-ever visible moment, or it started
				// hidden and this is the first time it has been shown.
				firstPollScheduled = true;
				scheduler.defer('machine-pressure:first', runFirstPoll);
			}
			// else: already queued and still waiting on the boot window: leave
			// it queued rather than firing a second poll here.
		} else {
			stopTimer();
		}
	};

	document.addEventListener('visibilitychange', onVisibilityChange);
	if (document.visibilityState === 'visible') {
		firstPollScheduled = true;
		scheduler.defer('machine-pressure:first', runFirstPoll);
	}

	return () => {
		stopTimer();
		document.removeEventListener('visibilitychange', onVisibilityChange);
	};
}
