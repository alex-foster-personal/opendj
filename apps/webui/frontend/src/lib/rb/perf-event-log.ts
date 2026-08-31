/**
 * Durable client-side ring log for performance timings + failures.
 * Survives the toast disappearing: localStorage + console + in-memory.
 * Inspect: localStorage mdt.perfEventLog or window.__mdtPerfLog().
 *
 * Load timings are always recorded (no opt-in flag). One compact
 * console.info line per load so DevTools filter `[perf]` is enough.
 *
 * TWO properties this module owes its callers, both of which a single global
 * FIFO with a synchronous write got wrong:
 *
 * 1. A NOISY KIND MUST NOT EVICT A QUIET ONE. `transport-schedule` is appended
 *    once per _scheduleDeck, and PitchFader drives that from an unthrottled
 *    pointermove - so one fader drag emitted ~40 rows and flushed every
 *    deck-load, deck-load-fail and audio-context row out of a 40-slot ring.
 *    __mdtLastLoads() then returned nothing, silently, exactly when someone was
 *    mid-session asking why the last load was slow. Each kind now gets its own
 *    budget and can only evict itself.
 *
 * 2. THE WRITE MUST NOT SIT ON THE GESTURE PATH. Every append used to
 *    JSON.stringify the whole ring and call localStorage.setItem synchronously,
 *    on the main thread, during that drag, while audio was playing. The
 *    in-memory array is authoritative and readable immediately; the flush to
 *    localStorage is coalesced onto a timer and forced on pagehide.
 */

export interface PerfEvent {
	t: string;
	kind: string;
	deck: 1 | 2 | 3 | 4 | null;
	message: string;
	/** Present for timing rows (ms per stage). */
	stages?: Record<string, number>;
}

const STORAGE_KEY = 'mdt.perfEventLog';

/**
 * Per-kind budgets. The ring is still bounded at the sum of these, so the
 * flushed JSON cannot grow; what changed is WHO pays for a burst.
 *
 * deck-load covers `deck-load sid=...` and `deck-load-fail`, i.e. the load KPI
 * __mdtLastLoads() reads. 16 rows is four full 4-deck loads.
 * transport-schedule is the latency instrument, and the noisy one: 16 rows is
 * the tail of one gesture, which is all a scheduled_offset_ms comparison needs.
 * Everything else (audio-context device floors, sync-failure, beat-sync-skip,
 * processor-latency-read-failed) is low volume and shares the remainder.
 */
const DECK_LOAD_BUDGET = 16;
const TRANSPORT_SCHEDULE_BUDGET = 16;
const OTHER_BUDGET = 8;

/** Trailing coalesce window for the localStorage write. */
const FLUSH_DEBOUNCE_MS = 250;

type PerfBucket = 'deck-load' | 'transport-schedule' | 'other';

const BUDGETS: Record<PerfBucket, number> = {
	'deck-load': DECK_LOAD_BUDGET,
	'transport-schedule': TRANSPORT_SCHEDULE_BUDGET,
	other: OTHER_BUDGET
};

/** Prefix match, because kinds carry a suffix (`deck-load sid=<id>`). */
function _bucketOf(kind: string): PerfBucket {
	if (kind.startsWith('deck-load')) return 'deck-load';
	if (kind.startsWith('transport-schedule')) return 'transport-schedule';
	return 'other';
}

/**
 * The newest rows each bucket is allowed to keep, still in chronological order.
 *
 * Walking from the newest backwards is what makes eviction oldest-first WITHIN a
 * bucket while leaving the other buckets untouched. The reverse at the end
 * restores the newest-last order that __mdtPerfLog() consumers rely on.
 */
function _withinBudgets(events: readonly PerfEvent[]): PerfEvent[] {
	const kept: PerfEvent[] = [];
	const taken: Record<PerfBucket, number> = {
		'deck-load': 0,
		'transport-schedule': 0,
		other: 0
	};
	for (let i = events.length - 1; i >= 0; i--) {
		const bucket = _bucketOf(events[i].kind);
		if (taken[bucket] >= BUDGETS[bucket]) continue;
		taken[bucket] += 1;
		kept.push(events[i]);
	}
	return kept.reverse();
}

let _events: PerfEvent[] = _withinBudgets(_readStorage());
/** Rows appended since the durable copy was last written. */
let _unflushed = false;
let _flushArmed = false;
let _pagehideInstalled = false;

function _readStorage(): PerfEvent[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw === null || raw === '') return [];
		const parsed: unknown = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		return parsed.filter(
			(e): e is PerfEvent =>
				typeof e === 'object' &&
				e !== null &&
				typeof (e as PerfEvent).t === 'string' &&
				typeof (e as PerfEvent).kind === 'string' &&
				typeof (e as PerfEvent).message === 'string'
		);
	} catch {
		return [];
	}
}

/**
 * Persist the ring now. Exported so a caller that cannot wait out the debounce
 * (page teardown, a test) can force the durable copy up to date.
 *
 * A flush with nothing outstanding is a no-op, which is what stops the armed
 * timer from writing a second identical blob behind a pagehide that already
 * took the same rows.
 */
export function flushPerfEventLog(): void {
	_flushArmed = false;
	if (!_unflushed) return;
	if (typeof localStorage === 'undefined') {
		_unflushed = false;
		return;
	}
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(_events));
		_unflushed = false;
	} catch {
		// Private mode / quota - console still has the line, and the rows stay
		// outstanding so the next flush retries rather than dropping them.
	}
}

/**
 * A navigation can arrive between the last append and the pending timer, and
 * pagehide is the last event that reliably fires for both a reload and a
 * bfcache suspend. Installed lazily on the first append (a ring nobody writes
 * to has nothing to lose) and exactly once, because the append path runs per
 * pointermove and a listener per row would leak a handler per sample.
 */
function _installFlushOnPagehide(): void {
	if (_pagehideInstalled) return;
	// A unit-test stand-in window can be a bare object; a real one always has
	// addEventListener, so this guard only ever skips a non-browser host.
	if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
	_pagehideInstalled = true;
	window.addEventListener('pagehide', flushPerfEventLog);
}

/**
 * Arm the coalesced write. Deliberately does NOT re-arm on every append: a
 * continuously fired gesture would otherwise keep pushing the deadline out and
 * the durable copy would never land while the drag lasted. One write per
 * FLUSH_DEBOUNCE_MS window bounds both the cost and the staleness.
 */
function _scheduleFlush(): void {
	_installFlushOnPagehide();
	if (_flushArmed) return;
	_flushArmed = true;
	setTimeout(flushPerfEventLog, FLUSH_DEBOUNCE_MS);
}

function _push(entry: PerfEvent): void {
	_events = _withinBudgets([..._events, entry]);
	_unflushed = true;
	_scheduleFlush();
}

function _stageSummary(stages: Record<string, number>): string {
	return Object.entries(stages)
		.map(([k, v]) => `${k}=${v}`)
		.join(' ');
}

/** Append a performance failure/event for after-the-fact diagnosis. */
export function recordPerfEvent(
	kind: string,
	message: string,
	deck: 1 | 2 | 3 | 4 | null = null
): void {
	_push({
		t: new Date().toISOString(),
		kind,
		deck,
		message
	});
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	console.warn(`[perf-event] ${kind}${deckBit}: ${message}`);
}

/** Always-on stage timing (ms). One console.info + ring entry. */
export function recordPerfTiming(
	kind: string,
	stages: Record<string, number>,
	deck: 1 | 2 | 3 | 4 | null = null
): void {
	const message = _stageSummary(stages);
	_push({
		t: new Date().toISOString(),
		kind,
		deck,
		message,
		stages: { ...stages }
	});
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	console.info(`[perf] ${kind}${deckBit} ${message}`);
}

export function readPerfEvents(): readonly PerfEvent[] {
	return _events;
}

/** Last deck-load timing row, newest first. Easy KPI paste for agents/CLI. */
export function lastDeckLoadEvents(limit = 4): readonly PerfEvent[] {
	const out: PerfEvent[] = [];
	for (let i = _events.length - 1; i >= 0 && out.length < limit; i--) {
		const e = _events[i];
		if (e.kind.startsWith('deck-load')) out.push(e);
	}
	return out;
}

/** DevTools helpers: __mdtPerfLog() full ring; __mdtLastLoads() recent deck loads. */
export function installPerfEventLogGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & {
		__mdtPerfLog?: () => readonly PerfEvent[];
		__mdtLastLoads?: (limit?: number) => readonly PerfEvent[];
	};
	w.__mdtPerfLog = () => readPerfEvents();
	w.__mdtLastLoads = (limit = 4) => lastDeckLoadEvents(limit);
}
