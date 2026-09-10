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

/**
 * The perf kinds whose ERROR-severity rows leave this browser.
 *
 * Deliberately a short, named allowlist rather than "everything at error
 * severity". `transport-schedule` is appended once per `_scheduleDeck` and
 * PitchFader drives that from an unthrottled pointermove, so a blanket rule
 * would put roughly one POST per pointer sample on the wire, during a set, on
 * the same main thread as the audio the escalation exists to protect.
 *
 * These are the audio-liveness kinds: each one means "the operator may be
 * hearing nothing, or seeing nothing move", and each is worth a round trip.
 * `audio-output-rebind-failed` joined them Wed 9 Sep 2026: the output-rebind
 * path was wired at `info` severity, so a failed re-bind - the operator
 * hearing nothing, recovery failed - stayed inside the browser.
 *
 * `audio-output-dead` and `audio-output-dead-persistent` joined them Thu 10
 * Sep 2026 (issue #1641): the app's only device-level "no sound is leaving
 * this machine" verdict, recorded at ERROR by audio-output-liveness.ts.
 * `escalated-kinds-superset.test.mjs` reds if any module records a kind at
 * `error` severity that is absent from this set, so this drift is CHECKED,
 * not just corrected.
 */
const ESCALATED_KINDS: ReadonlySet<string> = new Set([
	'xrun',
	'audio-context',
	'silent-while-playing',
	'presentation-clock-stalled',
	'presentation-stalled',
	'audio-output-rebind-failed',
	'audio-output-dead',
	'audio-output-dead-persistent'
]);

/**
 * At most one escalation per kind per window.
 *
 * A dropout is a SUSTAINED condition, not an event: the silence watchdog and
 * the presentation-stall watchdog are edge-triggered, but the xrun sentinel
 * reports every 2s for as long as the machine is struggling. Without this, a
 * twenty-minute incident is 600 POSTs describing the same twenty minutes.
 * Rate-limiting here rather than inside reportClientError keeps its 10s
 * fingerprint dedupe (which keys on the exact message) doing its own job:
 * these messages carry live numbers and so are never identical twice.
 */
const ESCALATION_WINDOW_MS = 60_000;

const _lastEscalationAtMs = new Map<string, number>();

/**
 * Where an escalated row goes, injected at client boot.
 *
 * THIS MODULE HAS NO IMPORTS, AND THAT IS A HARD PROPERTY, not a style
 * preference (the import statement that would prove it is deliberately not
 * written out here: the quality gate's dependency graph scans module TEXT, so
 * even a realistic import line inside a comment reads as a real edge). A
 * static import of `reportClientError` was tried Wed 2 Sep 2026; it reaches
 * `$lib/api/client.ts`, which evaluates `import.meta.env.VITE_API_BASE` at
 * module scope, undefined under Playwright's plain-Node spec loader - every
 * Playwright config whose specs reach this module transitively died at CONFIG
 * LOAD, reporting zero tests rather than a failure. A sink injected at boot
 * keeps the escalation and keeps the purity.
 *
 * `null` is a legitimate state, not an error: unit tests, Playwright's Node
 * loader and any pre-boot code all run without a sink, and a row must never
 * throw its way out of a logger.
 */
let _escalator: ((event: PerfEvent) => void) | null = null;
let _warnedNoEscalator = false;

/**
 * Point escalated rows at a sink. Called once, at client boot.
 *
 * Takes the row rather than a pre-formatted message so this module keeps
 * ownership of its own shape and the sink decides what to do with it. `null`
 * unsets it, which is what lets a test exercise the unwired path rather than
 * assume it.
 */
export function setPerfEventEscalator(sink: ((event: PerfEvent) => void) | null): void {
	_escalator = sink;
}

export interface PerfEvent {
	t: string;
	kind: string;
	deck: 1 | 2 | 3 | 4 | null;
	message: string;
	/** Present for timing rows (ms per stage). */
	stages?: Record<string, number>;
	/**
	 * Non-numeric facts about the row, e.g. which stem layout a deck load
	 * actually played. Optional and additive: rows written before this field
	 * existed stay valid, and readers must treat it as possibly absent.
	 *
	 * Kept SEPARATE from `stages` rather than stringly-typed into it, because
	 * `stages` is a ms-per-stage map and anything summing or charting it must
	 * never trip over a value that is not a duration.
	 */
	labels?: Record<string, string>;
	/**
	 * Correlation id for rows that something on screen also shows.
	 *
	 * A toast prints this id and copies it to the clipboard, so the id here and
	 * the id the user pasted into an issue are the same string by construction.
	 * Without it, two identical `toast-error` rows ten minutes apart are
	 * indistinguishable and the copied id refers to nothing.
	 *
	 * Optional and additive: rows written before this field existed stay valid,
	 * and rows nothing displays (deck-load timings) have no id to carry.
	 */
	id?: string;
}

const STORAGE_KEY = 'mdt.perfEventLog';
const DECK_STATE_STORAGE_KEY = 'mdt.deckState';

/**
 * The durable "all four decks were created empty" stamp, written once per page
 * load.
 *
 * The initial deck state is a CONSTANT, so recording it as four ring rows on
 * every load spent shared-budget rows restating a fact and evicted real
 * diagnostics. It lives here instead, as a single non-ring record the resource
 * probe reads to tell "decks are unloaded" from "the ring has no deck-state
 * evidence". `t` doubles as the reset gate: ring rows OLDER than it describe an
 * earlier page session whose deck states this page-load reset discarded.
 */
export interface DeckStateBaseline {
	/** Shape version. */
	v: 1;
	/** ISO instant the four decks were created empty. */
	t: string;
	/** The decks that were empty at `t`. */
	unloaded: [1, 2, 3, 4];
}

/**
 * Per-kind budgets. The ring is still bounded at the sum of these, so the
 * flushed JSON cannot grow; what changed is WHO pays for a burst.
 * deck-load covers `deck-load sid=...` and `deck-load-fail`, i.e. the load KPI
 * __mdtLastLoads() reads. 16 rows is four full 4-deck loads.
 * transport-schedule is the latency instrument, and the noisy one: 16 rows is
 * the tail of one gesture, which is all a scheduled_offset_ms comparison needs.
 * deck-state covers `deck-state-empty` (legacy empty-deck boot rows) and
 * `deck-unload`, i.e. the rows the resource probe reconstructs deck state
 * from. Deck churn must be able neither to evict the audio-liveness kinds below
 * nor to be evicted by them, so it is not part of the shared remainder; 8 rows
 * is two full unload cycles.
 * Everything else (audio-context device floors, sync-failure, beat-sync-skip,
 * processor-latency-read-failed) is low volume and shares the remainder.
 */
const DECK_LOAD_BUDGET = 16;
const TRANSPORT_SCHEDULE_BUDGET = 16;
const DECK_STATE_BUDGET = 8;
const OTHER_BUDGET = 8;
/**
 * Q1: press rows, kept out of the pitch fader's way. PitchFader drives
 * `_scheduleDeck` from an unthrottled pointermove, so one drag is ~40 rows
 * against 16. A DJ starts a track and then reaches for the fader to
 * beatmatch it - the standard gesture, not an edge case - so the press row
 * carrying `input_to_audible_ms` was evicted by the operator's very next
 * move. 8 is two full four-deck press flurries, which is all a press
 * comparison needs: a press is a discrete gesture, never a per-frame stream.
 */
const TRANSPORT_PRESS_BUDGET = 8;

/** Trailing coalesce window for the localStorage write. */
const FLUSH_DEBOUNCE_MS = 250;

type PerfBucket =
	| 'deck-load'
	| 'transport-schedule'
	| 'transport-schedule-press'
	| 'deck-state'
	| 'other';

const BUDGETS: Record<PerfBucket, number> = {
	'deck-load': DECK_LOAD_BUDGET,
	'transport-schedule': TRANSPORT_SCHEDULE_BUDGET,
	'transport-schedule-press': TRANSPORT_PRESS_BUDGET,
	'deck-state': DECK_STATE_BUDGET,
	other: OTHER_BUDGET
};

/** Prefix match, because kinds carry a suffix (`deck-load sid=<id>`).
 * `deck-unload` is matched exactly: it shares the deck-state bucket with the
 * `deck-state-*` kinds but predates the `deck-state-` prefix on its own kind.
 *
 * ORDER IS LOAD-BEARING below: press kinds are a SUFFIX of the plain one, so
 * testing `transport-schedule` first would swallow every press row back into
 * the fader's bucket. The suffix shape keeps existing prefix consumers whole. */
function _bucketOf(kind: string): PerfBucket {
	if (kind.startsWith('deck-load')) return 'deck-load';
	if (kind.startsWith('transport-schedule-press')) return 'transport-schedule-press';
	if (kind.startsWith('transport-schedule')) return 'transport-schedule';
	if (kind.startsWith('deck-state') || kind === 'deck-unload') return 'deck-state';
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
		'transport-schedule-press': 0,
		'deck-state': 0,
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
 * Record that the player's four decks were just created empty (page-load
 * reset) as ONE durable baseline, not four ring rows.
 *
 * Call from the module that owns the empty deck states, once per page load.
 * Written synchronously because it is one small write on the boot path, not a
 * row on a gesture path; a missing baseline degrades to the probe reporting
 * "no deck-state evidence", never to a fabricated clean unload.
 */
export function recordDeckStateBaseline(): void {
	if (typeof localStorage === 'undefined') return;
	const stamp: DeckStateBaseline = {
		v: 1,
		t: new Date().toISOString(),
		unloaded: [1, 2, 3, 4]
	};
	try {
		localStorage.setItem(DECK_STATE_STORAGE_KEY, JSON.stringify(stamp));
	} catch {
		// Private mode / quota: load/unload ring rows still describe state
		// changes, and the probe already reports a missing baseline as such.
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

/**
 * Append a performance failure/event for after-the-fact diagnosis.
 *
 * RETURNS THE ROW IT WROTE, which is what lets a caller stamp the SAME instant
 * onto whatever it puts on screen. A toast that called `new Date()` of its own
 * would print a timestamp a millisecond off the row's, and a reader comparing
 * the two would be left wondering whether they were even the same event.
 *
 * `id` is printed into the console line as well as stored, because the console
 * is where somebody actually greps: a structured field nothing prints is a
 * field that only helps whoever already knew to open localStorage.
 */
/**
 * Forward one audio-liveness failure to the engine, at most once per window.
 *
 * Additive, never a re-route: the row is already in the ring and on the
 * console before this runs, and a failure here cannot remove it.
 * `reportClientError` owns its own durable retry queue, so a POST that fails
 * is retried on the next report or page load rather than lost.
 */
function _escalate(entry: PerfEvent): void {
	if (!ESCALATED_KINDS.has(entry.kind)) return;
	if (_escalator === null) {
		// Once per session, not per row: a sustained dropout would otherwise turn
		// a missing sink into its own console flood. The row is already in the
		// ring and on the console, so nothing is lost but the round trip.
		if (!_warnedNoEscalator) {
			_warnedNoEscalator = true;
			console.warn(
				'[perf-event] no escalator wired; audio-liveness failures stay in this browser. ' +
					'setPerfEventEscalator() is called from installClientErrorReporting() at ' +
					'client boot, so this is expected under tests and outside the browser.'
			);
		}
		return;
	}
	const nowMs = Date.now();
	const lastMs = _lastEscalationAtMs.get(entry.kind);
	if (lastMs !== undefined && nowMs - lastMs < ESCALATION_WINDOW_MS) return;
	_lastEscalationAtMs.set(entry.kind, nowMs);
	_escalator(entry);
}

export function recordPerfEvent(
	kind: string,
	message: string,
	deck: 1 | 2 | 3 | 4 | null = null,
	severity: 'info' | 'warn' | 'error' = 'warn',
	id?: string
): PerfEvent {
	const entry: PerfEvent = {
		t: new Date().toISOString(),
		kind,
		deck,
		message,
		...(id === undefined ? {} : { id })
	};
	_push(entry);
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	const idBit = id === undefined ? '' : ` id=${id}`;
	console[severity](`[perf-event] ${kind}${deckBit}${idBit}: ${message}`);
	// AFTER the ring append and the console line, so an escalation that throws
	// cannot cost the local record that is the last resort when the network is
	// the thing that is broken.
	if (severity === 'error') _escalate(entry);
	return entry;
}

/**
 * The ring row carrying this correlation id, or null.
 *
 * This is the lookup that proves the id on screen is not decorative: an agent
 * or a test copies the id out of a toast and asks the log for it directly,
 * rather than eyeballing two lists side by side.
 */
export function findPerfEventById(id: string): PerfEvent | null {
	for (let i = _events.length - 1; i >= 0; i--) {
		if (_events[i].id === id) return _events[i];
	}
	return null;
}

/** Always-on stage timing (ms). One console.info + ring entry. */
export function recordPerfTiming(
	kind: string,
	stages: Record<string, number>,
	deck: 1 | 2 | 3 | 4 | null = null,
	labels?: Record<string, string>
): void {
	const stageBits = _stageSummary(stages);
	const labelBits =
		labels === undefined
			? ''
			: Object.entries(labels)
					.map(([k, v]) => `${k}=${v}`)
					.join(' ');
	const message = labelBits === '' ? stageBits : `${stageBits} ${labelBits}`;
	_push({
		t: new Date().toISOString(),
		kind,
		deck,
		message,
		stages: { ...stages },
		...(labels === undefined ? {} : { labels: { ...labels } })
	});
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	console.info(`[perf] ${kind}${deckBit} ${message}`);
}

// ------------------------------------------------- deck-load stem telemetry

/**
 * The subset of a deck's StemDeckState a ring row needs, declared structurally
 * rather than imported.
 *
 * Deliberate: `types.ts` is already the most-imported module in the frontend
 * and the quality ratchet caps its fan-in, so this module stays off it - the
 * same reason the deck id above is an inline union rather than an imported
 * DeckId. A real StemDeckState satisfies this shape, and if its status union
 * ever widens, the call site in audio-engine stops compiling, so the
 * exhaustive switch below cannot silently fall behind.
 */
export interface StemLoadFacts {
	status: 'unavailable' | 'loading' | 'ready' | 'error';
	source: string | null;
	model: string | null;
	layout: string | null;
	error: string | null;
}

interface _StemLoadTelemetry {
	/** 1 stemmed, 0 mix-only, null when the probe never resolved. */
	stemmed: number | null;
	labels: Record<string, string>;
}

/** Derive a row's stem facts from the state the deck actually published.
 *
 * Exhaustive over the status union: an unrecognized status throws rather than
 * defaulting to mix-only, because a row that quietly claims stemmed=0 for an
 * unknown state is worse than no row at all. */
function _stemLoadTelemetry(stems: StemLoadFacts): _StemLoadTelemetry {
	switch (stems.status) {
		case 'ready':
			// Read from the bundle the deck published, never assumed to be the
			// 4-part default: a row claiming demucs4 for a 2-part RoFormer bundle
			// would misreport which controls the deck could actually drive.
			return {
				stemmed: 1,
				labels: {
					stemLayout: stems.layout ?? 'unknown',
					stemSource: stems.source ?? 'unknown',
					stemModel: stems.model ?? 'unknown'
				}
			};
		case 'loading':
			// Mix-first load: the deck is playable now and the stem bundle is still
			// in flight, so at THIS row's write time there is no stemmed verdict to
			// record. Withheld rather than written as 0, for the same reason as the
			// unresolved case below: only a settled probe answer earns stemmed=0.
			// The outcome lands on the separate deck-stems / deck-stems-none /
			// deck-stems-fail rows the upgrade emits.
			return { stemmed: null, labels: { stemLayout: 'pending' } };
		case 'unavailable':
			// A null error means the probe never answered - the load failed before
			// it resolved. That is NOT mix-only, so the flag is withheld rather
			// than fabricated; only a real probe answer earns stemmed=0.
			if (stems.error === null) return { stemmed: null, labels: { stemLayout: 'unresolved' } };
			// Explicit 'none', not an absent key: that is what lets a reader tell a
			// mix-only load apart from a row written before this field existed.
			return { stemmed: 0, labels: { stemLayout: 'none' } };
		case 'error':
			// Stems existed but were unusable, so the deck played the mix. The
			// layout records WHY rather than losing the distinction.
			return { stemmed: 0, labels: { stemLayout: 'error' } };
		default: {
			const _exhaustive: never = stems.status;
			throw new Error(`deck-load telemetry: unhandled stem status ${String(_exhaustive)}`);
		}
	}
}

/**
 * Write one deck-load timing row carrying the stem facts alongside the ms.
 *
 * A deck-load row used to carry stage DURATIONS only, so "was that load
 * stemmed or mix-only?" had no answer in the row, and classifying a run of
 * loads meant re-deriving it by hand from which stage names happened to appear
 * (fetchStems/decodeStems present => stemmed). This states it at write time.
 *
 * Lives here beside `lastDeckLoadEvents`, the ring's other deck-load-specific
 * helper, rather than in the engine: audio-engine.svelte.ts is the largest
 * hand-written frontend module and sits AT the ratchet's file_size.max_frontend
 * cap, which ops/quality/baseline.json says must never be raised for
 * hand-written code, so it can absorb no new lines at all.
 *
 * `stages` is copied, never mutated: the engine snapshots it into DeckState
 * BEFORE the ring write, and a mutating recorder would make those two disagree
 * depending on statement order.
 *
 * `extraLabels` merges in facts this module has no business deriving; omitted, the row is unchanged from before this parameter existed.
 */
export function recordDeckLoadTiming(
	kind: string,
	stages: Record<string, number>,
	deck: 1 | 2 | 3 | 4 | null,
	stems: StemLoadFacts, extraLabels?: Record<string, string>
): void {
	const { stemmed, labels } = _stemLoadTelemetry(stems);
	recordPerfTiming(kind, stemmed === null ? stages : { ...stages, stemmed }, deck, extraLabels === undefined ? labels : { ...labels, ...extraLabels });
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
