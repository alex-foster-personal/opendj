/**
 * WHO PAYS FOR A BURST: the perf ring's retention policy, and the row shape
 * that policy is about.
 *
 * Split out of `perf-event-log.ts` under convention D5, for the same reason
 * `silence-watchdog.ts` is split from `master-silence-report.ts`: everything
 * here is a PURE DECISION over rows, while the module it came from owns the
 * storage, the console line and the escalation. The decision is the half worth
 * testing directly, and the half `audio-health-mirror.ts` has to agree with
 * exactly.
 */

export interface PerfEvent {
	t: string;
	kind: string;
	deck: 1 | 2 | 3 | 4 | null;
	message: string;
	/**
	 * The severity the recording site judged, PERSISTED.
	 *
	 * It used to reach only `console[severity]` and the escalation gate, so a
	 * reader of the durable ring could not tell `audio-output-alive` (a recovery)
	 * from `audio-output-dead` (an outage) by anything but the name. Retention
	 * and the agent-facing fault timeline both key on it now, so it has to
	 * survive the round trip through localStorage.
	 *
	 * Optional and additive: rows written before this field existed stay valid
	 * and read as UNKNOWN severity, never as healthy.
	 */
	severity?: 'info' | 'warn' | 'error';
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
/**
 * Audio-health FAULTS, kept out of the shared remainder.
 *
 * These are the rows `audio-health-mirror.ts` publishes as `recent_faults`,
 * and the whole reason that field reads the DURABLE ring rather than the live
 * toast store is that a fault must stay legible long after its toast is gone.
 * In the shared `other` bucket it was not: eight rows, shared with toast rows,
 * device-floor rows and the SUCCESSFUL states `audio-output-alive` and
 * `audio-output-rebound`, so eight ordinary events after an outage evicted the
 * outage. A dedicated budget is what makes the durability claim true.
 *
 * The healthy audio rows stay OUT of it (`audioHealthFaultSeverity` below):
 * admitting them would reproduce the same eviction one bucket over, with 16
 * `audio-output-alive` rows flushing the outage they are the recovery from.
 */
const AUDIO_HEALTH_BUDGET = 16;
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
/**
 * LATENCY-03: EQ knob rows, kept out of transport-schedule and the shared
 * remainder. One drag is many pointermove applies; 16 rows is the tail of one
 * gesture, same size as transport-schedule.
 */
const EQ_APPLY_BUDGET = 16;

type PerfBucket =
	| 'deck-load'
	| 'transport-schedule'
	| 'transport-schedule-press'
	| 'eq-apply'
	| 'deck-state'
	| 'audio-health'
	| 'other';

const BUDGETS: Record<PerfBucket, number> = {
	'deck-load': DECK_LOAD_BUDGET,
	'transport-schedule': TRANSPORT_SCHEDULE_BUDGET,
	'transport-schedule-press': TRANSPORT_PRESS_BUDGET,
	'eq-apply': EQ_APPLY_BUDGET,
	'deck-state': DECK_STATE_BUDGET,
	'audio-health': AUDIO_HEALTH_BUDGET,
	other: OTHER_BUDGET
};

/**
 * Kinds that belong to the audio-health DOMAIN, and the ONE definition of it.
 *
 * Domain only. It says a row is about audio output, never that anything is
 * wrong: `audio-output-alive` and `audio-context` are audio-domain rows that
 * report health. `audioHealthFaultSeverity` below is what separates the two, and it is the
 * only export: a second predicate answering a DIFFERENT question is exactly
 * how the ring's retention and the mirror's selection drift apart.
 *
 * `xrun` is deliberately excluded despite being audio: it fires continuously
 * (34 times in one morning) and would crowd out the rare rows that matter. It
 * has its own counter in the mirror under `xrun_sentinel`.
 */
const AUDIO_HEALTH_EXTRA_KINDS: ReadonlySet<string> = new Set([
	'silent-while-playing',
	'stranded-follower',
	'presentation-clock-stalled',
	'presentation-clock-recovered',
	'presentation-tick-failed'
]);

function _isAudioHealthKind(kind: string): boolean {
	return kind.startsWith('audio-') || AUDIO_HEALTH_EXTRA_KINDS.has(kind);
}

/**
 * The fault severity of an audio-health row, or null when the row is not a
 * fault at all. ONE definition, exported, for the same reason the domain
 * predicate is: `audio-health-mirror.ts` selects exactly the rows this module
 * reserves durable capacity for, and two copies of the rule would let the rows
 * the ring KEEPS and the rows the mirror LOOKS FOR drift apart.
 *
 * WHY SEVERITY AND NOT A NAME LIST (Codex, Thu 10 Sep 2026). The dedicated
 * budget exists so that an outage row survives later traffic. Keying it on the
 * `audio-` prefix alone put the SUCCESSFUL rows in there too, so a healthy
 * startup published a nonempty `recent_faults` and 16 later `audio-output-alive`
 * rows could evict the one outage the bucket was reserved for. Severity is
 * already the recorded judgement of the site that knows - and unlike a curated
 * list of fault names, a new audio failure kind is covered the day it is
 * written, with no second place to remember to edit.
 *
 * `undefined` is a row written before severity was persisted. It is UNKNOWN,
 * not healthy, so it is kept and mirrored rather than dropped - an unmeasured
 * row must never render as a good one - and it is labelled `unknown` in the
 * mirror so a reader can tell a diagnosed fault from a legacy row.
 */
export function audioHealthFaultSeverity(event: PerfEvent): 'warn' | 'error' | 'unknown' | null {
	if (!_isAudioHealthKind(event.kind)) return null;
	if (event.severity === 'info') return null;
	// Anything that is not one of the three recorded levels came out of
	// localStorage, which this module does not control; it is UNKNOWN, and it
	// is never allowed to widen the type by being returned verbatim.
	if (event.severity === 'warn' || event.severity === 'error') return event.severity;
	return 'unknown';
}

/** Prefix match, because kinds carry a suffix (`deck-load sid=<id>`).
 * `deck-unload` is matched exactly: it shares the deck-state bucket with the
 * `deck-state-*` kinds but predates the `deck-state-` prefix on its own kind.
 *
 * ORDER IS LOAD-BEARING below: press kinds are a SUFFIX of the plain one, so
 * testing `transport-schedule` first would swallow every press row back into
 * the fader's bucket. The suffix shape keeps existing prefix consumers whole. */
function _bucketOf(event: PerfEvent): PerfBucket {
	const kind = event.kind;
	if (audioHealthFaultSeverity(event) !== null) return 'audio-health';
	if (kind.startsWith('deck-load')) return 'deck-load';
	if (kind.startsWith('transport-schedule-press')) return 'transport-schedule-press';
	if (kind.startsWith('transport-schedule')) return 'transport-schedule';
	if (kind === 'eq-apply') return 'eq-apply';
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
export function withinBudgets(events: readonly PerfEvent[]): PerfEvent[] {
	const kept: PerfEvent[] = [];
	const taken: Record<PerfBucket, number> = {
		'deck-load': 0,
		'transport-schedule': 0,
		'transport-schedule-press': 0,
		'eq-apply': 0,
		'deck-state': 0,
		'audio-health': 0,
		other: 0
	};
	for (let i = events.length - 1; i >= 0; i--) {
		const bucket = _bucketOf(events[i]);
		if (taken[bucket] >= BUDGETS[bucket]) continue;
		taken[bucket] += 1;
		kept.push(events[i]);
	}
	return kept.reverse();
}
