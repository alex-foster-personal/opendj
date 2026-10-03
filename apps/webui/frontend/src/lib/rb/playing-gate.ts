/**
 * PERFMODE-04 demand shedding, first real plumbing: hold non-critical work off
 * the main thread while a deck is playing, and never drop it.
 *
 * WHY THIS EXISTS. Background work in this app is event-driven, which is the
 * right design and also why it lands at the worst possible moment: a rating
 * PATCH, a bulk edit, a find/replace or a WS reconnect all publish
 * `library.changed`, and the pane's answer to that is a full refetch plus a
 * whole-list re-render. Off-set that is invisible. Mid-mix it is a main-thread
 * stall competing with the audio graph, and the DJ feels it. The invalidation
 * itself is honest and must not be discarded; what it must not do is choose the
 * instant it is serviced.
 *
 * THE CONTRACT (fail-fast, never silently lossy):
 *   - nothing playing        -> the work runs immediately, exactly as before
 *   - a deck playing         -> one dirty flag is raised, the work is owed
 *   - playback stops         -> the owed work runs, at once
 *   - the user touches the
 *     gated surface          -> the owed work runs even though audio is live,
 *                               because touching the library IS a request for
 *                               fresh rows, but yielded to an idle slot rather
 *                               than run on the gesture path
 *
 * A deferral is never a drop: the flag guarantees eventual execution, and many
 * triggers arriving during one episode collapse into the single owed run rather
 * than queueing.
 *
 * WHEN, AND HOW MANY TIMES, IN ONE PLACE. The gate owns `coalesce` rather than
 * sitting on top of a separately wired one: every background job worth shedding
 * needs both answers, and splitting them across two call sites is how a later
 * caller ends up with one and not the other. A release therefore cannot start a
 * second concurrent run, and a trigger that lands mid-run still books exactly
 * one trailing pass (see `$lib/rb/coalesce`).
 *
 * Deliberately generic and injectable. Everything PERFMODE-04 goes on to shed
 * (prefetch caps, background workers, waveform detail, poll cadence - QUEUE.md
 * Q28) wants exactly this shape, and a gate whose clock, scheduler and ring
 * writer are parameters is one a unit test can drive without a browser.
 */
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import { coalesce } from '$lib/rb/coalesce';
import { S1_XRUN_DELTA_MAX, subscribeMachinePressure } from '$lib/rb/machine-pressure';
import { recordPerfTiming } from '$lib/rb/perf-event-log';

/**
 * Deadline on the idle slot an interaction-driven release waits for.
 *
 * A user who just scrolled the pane is waiting for rows, and audio keeps the
 * main thread busy enough that an unbounded idle queue could starve the work
 * for seconds. Short enough to feel immediate, long enough to miss the frame
 * the gesture itself is being painted on.
 */
const INTERACTIVE_IDLE_TIMEOUT_MS = 200;

/**
 * The cheapest authoritative "the set is live" read, and the one signal every
 * PERFMODE-04 shed should key off.
 *
 * Reads the engine's published deck state directly rather than adding an
 * accessor to audio-engine.svelte.ts, which sits AT the ratchet's
 * file_size.max_frontend cap and can absorb no new lines.
 *
 * Called from inside a `$effect` this subscribes that effect to every deck's
 * transport, so a gate owner gets its drain for free with no polling.
 */
export function anyDeckPlaying(): boolean {
	for (const deck of DECK_IDS) {
		const state = deckStates[deck];
		// Both flags, not either alone. `playing` is the desired/scheduled
		// transport and LEADS `audible` by the schedule lead, so a deck whose
		// play has been dispatched but has not reached the output yet is already
		// live; `audible` outlives `playing` for a stop that is still ringing
		// out. Watching one of them opens a window at exactly the start or end
		// of a mix, which is the worst place to start a refetch.
		if (state.playing || state.audible) return true;
	}
	return false;
}

/** Defers a task to a moment the main thread is not needed for audio. */
type IdleScheduler = (task: () => void) => void;

/**
 * requestIdleCallback where the host has it, else the next macrotask.
 *
 * Not a silent fallback masking a failure: a macrotask still gets the work off
 * the gesture path, which is the whole point of yielding here. WKWebView only
 * grew requestIdleCallback recently, so the packaged app can legitimately land
 * on either branch.
 */
const _scheduleWhenIdle: IdleScheduler = (task: () => void): void => {
	const host = globalThis as typeof globalThis & {
		requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
	};
	if (typeof host.requestIdleCallback === 'function') {
		host.requestIdleCallback(() => task(), { timeout: INTERACTIVE_IDLE_TIMEOUT_MS });
	} else {
		setTimeout(task, 0);
	}
};

/** What ended a deferral episode. Recorded as a ring label. */
export type GateResume = 'playback-stopped' | 'user-interaction' | 'pressure-cleared';

/** Ordered non-P0 jobs shed under pressure while a deck is playing (lowest priority first). */
export const BACKGROUND_SHED_JOBS = [
	'library-poll-cadence',
	'background-workers',
	'waveform-detail-bands',
	'audio-prefetch-cache-caps',
	'eager-stem-decode',
	'cloudsync-scheduler'
] as const;

export type BackgroundShedJobId = (typeof BACKGROUND_SHED_JOBS)[number];

/** P0 paths that must never be registered on the shed list. */
export const P0_NEVER_SHED = ['audio-callbacks', 'track-select', 'transport'] as const;

/** Toast suggestion plumbing only: names actions, does not toggle them. */
export const SHED_TOAST_SUGGESTIONS = [
	{ action: '2-channel-mode', message: 'Suggest 2-channel mode' },
	{ action: 'live-generation-off', message: 'Suggest live-generation off' }
] as const;

interface PlayingGateOptions {
	/** The shed work. Coalesced by the gate, so releases never overlap. */
	run: () => Promise<void>;
	/** Cheapest authoritative live-transport read, normally {@link anyDeckPlaying}. */
	isPlaying: () => boolean;
	/** Ring kind for the single row a deferral episode writes. */
	kind: string;
	/** Injected by tests; production takes the real scheduler, clock and ring. */
	schedule?: IdleScheduler | undefined;
	now?: (() => number) | undefined;
	record?: typeof recordPerfTiming | undefined;
}

interface PlayingGate {
	/** Background trigger: runs now when idle, is owed while a deck plays. */
	request(): void;
	/** Transport may have stopped - release the owed run if nothing is live. */
	drain(resumedBy?: GateResume): void;
	/** The user touched the gated surface: release now, off the gesture path. */
	flushOnInteraction(): void;
	/** True while a release is owed. */
	readonly pending: boolean;
}

export function createPlayingGate(options: PlayingGateOptions): PlayingGate {
	const schedule = options.schedule ?? _scheduleWhenIdle;
	const now = options.now ?? ((): number => Date.now());
	const record = options.record ?? recordPerfTiming;
	// A rejection is deliberately left to propagate out of the trigger promise
	// rather than swallowed here: a refresh that throws is a real failure and
	// the work it wraps is the only layer that knows what to do about it.
	const trigger = coalesce(options.run);

	let pending = false;
	let deferredSince = 0;
	let coalesced = 0;

	/**
	 * Close the episode and do the work.
	 *
	 * ONE ring row per deferral EPISODE, written as it closes rather than as it
	 * opens: that is the only moment both numbers exist (how long the set was
	 * shielded, and how many invalidations collapsed into the single run), and
	 * it bounds the emission rate by playback stops instead of by the
	 * invalidation burst that fed the episode. The ring holds 40 rows, so a
	 * per-trigger emitter would destroy the log it writes to.
	 */
	function _release(resumedBy: GateResume, yieldFirst: boolean): void {
		const stages: Record<string, number> = {
			deferredMs: Math.round(now() - deferredSince),
			coalesced
		};
		pending = false;
		deferredSince = 0;
		coalesced = 0;
		record(options.kind, stages, null, { resumedBy });
		if (yieldFirst) schedule(() => void trigger());
		else void trigger();
	}

	return {
		request(): void {
			if (options.isPlaying()) {
				coalesced += 1;
				if (pending) return;
				pending = true;
				deferredSince = now();
				return;
			} else if (pending) {
				// Transport stopped between the deferral and this trigger, and the
				// drain has not run yet. Close the episode rather than running the
				// work now and leaving the flag up for a second, redundant run.
				coalesced += 1;
				_release('playback-stopped', false);
				return;
			}
			void trigger();
		},
		drain(resumedBy: GateResume = 'playback-stopped'): void {
			if (!pending) return;
			if (options.isPlaying()) return;
			_release(resumedBy, false);
		},
		flushOnInteraction(): void {
			// First line on purpose: this is wired to scroll and row-select, so
			// the no-work path has to be one boolean read.
			if (!pending) return;
			_release('user-interaction', true);
		},
		get pending(): boolean {
			return pending;
		}
	};
}

interface BackgroundDemandShedJob {
	id: BackgroundShedJobId;
	run: () => Promise<void>;
}

interface BackgroundDemandShedOptions {
	isPlaying: () => boolean;
	pressureElevated: () => boolean;
	readXruns: () => number;
	jobs: ReadonlyArray<BackgroundDemandShedJob>;
	notify?: ((suggestion: { action: string; message: string }) => void) | undefined;
	schedule?: IdleScheduler | undefined;
	now?: (() => number) | undefined;
	record?: typeof recordPerfTiming | undefined;
}

export interface BackgroundDemandShed {
	request(id: BackgroundShedJobId): void;
	sync(): void;
	readonly pending: boolean;
	/** PERFMODE-18: an xrun landed since the last sync. Audio is being
	 * damaged now, which a consumer may weigh above an early warning. */
	readonly xrunsInWindow: boolean;
}

function _assertShedJobId(id: string): void {
	if ((P0_NEVER_SHED as readonly string[]).includes(id)) {
		throw new Error(`P0 job ${id} cannot be shed`);
	}
	if (!(BACKGROUND_SHED_JOBS as readonly string[]).includes(id)) {
		throw new Error(`Unknown shed job id: ${id}`);
	}
}

/**
 * Ordered background-demand shed keyed off pressure, xrun window delta, and
 * live transport. Non-P0 work is owed while elevated, never dropped, and runs
 * once coalesced when signals return to normal.
 */
export function createBackgroundDemandShed(
	options: BackgroundDemandShedOptions
): BackgroundDemandShed {
	const jobRuns = new Map<BackgroundShedJobId, () => Promise<void>>();
	for (const job of options.jobs) {
		_assertShedJobId(job.id);
		jobRuns.set(job.id, job.run);
	}

	const owed = new Set<BackgroundShedJobId>();
	let xrunsAtPreviousSync = options.readXruns();
	let wasDeferring = false;
	let toastFiredThisEpisode = false;

	const gate = createPlayingGate({
		kind: 'background-demand-shed',
		isPlaying: () => options.isPlaying() && _isElevated(),
		run: async () => {
			for (const id of BACKGROUND_SHED_JOBS) {
				if (!owed.has(id)) continue;
				const run = jobRuns.get(id);
				if (run !== undefined) await run();
				owed.delete(id);
			}
		},
		schedule: options.schedule,
		now: options.now,
		record: options.record
	});

	function _xrunWindowDelta(): number {
		return options.readXruns() - xrunsAtPreviousSync;
	}

	function _isElevated(): boolean {
		return options.pressureElevated() || _xrunWindowDelta() > S1_XRUN_DELTA_MAX;
	}

	function _shouldDefer(): boolean {
		return options.isPlaying() && _isElevated();
	}

	return {
		request(id: BackgroundShedJobId): void {
			_assertShedJobId(id);
			if (_shouldDefer()) {
				owed.add(id);
				wasDeferring = true;
				gate.request();
				return;
			}
			const run = jobRuns.get(id);
			if (run !== undefined) void run();
		},
		sync(): void {
			const deferring = _shouldDefer();
			if (deferring && !wasDeferring) {
				if (!toastFiredThisEpisode) {
					for (const suggestion of SHED_TOAST_SUGGESTIONS) {
						options.notify?.(suggestion);
					}
					toastFiredThisEpisode = true;
				}
			}
			if (!deferring) toastFiredThisEpisode = false;
			if (wasDeferring && !deferring && (gate.pending || owed.size > 0)) {
				gate.drain('pressure-cleared');
			}
			wasDeferring = deferring;
			xrunsAtPreviousSync = options.readXruns();
		},
		get pending(): boolean {
			return owed.size > 0 || gate.pending;
		},
		get xrunsInWindow(): boolean {
			return _xrunWindowDelta() > S1_XRUN_DELTA_MAX;
		}
	};
}

export interface StartBackgroundDemandShedOptions {
	isPlaying?: (() => boolean) | undefined;
	pressureElevated?: (() => boolean) | undefined;
	readXruns?: (() => number) | undefined;
	notify?: ((suggestion: { action: string; message: string }) => void) | undefined;
	jobs?: ReadonlyArray<BackgroundDemandShedJob> | undefined;
	onShed?: ((shed: BackgroundDemandShed) => void) | undefined;
	schedule?: IdleScheduler | undefined;
	now?: (() => number) | undefined;
	record?: typeof recordPerfTiming | undefined;
}

/** Arm the pressure shed for the page lifetime. Returns teardown. */
export function startBackgroundDemandShed(
	options: StartBackgroundDemandShedOptions = {}
): () => void {
	const shed = createBackgroundDemandShed({
		isPlaying: options.isPlaying ?? anyDeckPlaying,
		pressureElevated: options.pressureElevated ?? ((): boolean => false),
		readXruns: options.readXruns ?? ((): number => 0),
		notify: options.notify,
		jobs: options.jobs ?? [],
		schedule: options.schedule,
		now: options.now,
		record: options.record
	});
	options.onShed?.(shed);
	const unsubscribe = subscribeMachinePressure(() => shed.sync());
	shed.sync();
	return unsubscribe;
}
