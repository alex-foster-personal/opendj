/**
 * The conditions a deck-load timing row is measured under, attached to the row.
 *
 * A number without its conditions is an anecdote. This module is the one place
 * that knows both halves -- which other loads were in flight (deck-load-
 * concurrency.ts) and what the machine was doing (machine-pressure.ts) -- and
 * it stamps them onto the row that perf-event-log.ts writes.
 *
 * WHY IT IS NOT IN THE ENGINE. audio-engine.svelte.ts is the largest
 * hand-written frontend module and sits AT the quality ratchet's
 * file_size.max_frontend cap (4069 of 4069 measured on main, Wed 9 Sep 2026),
 * so it can absorb no new lines at all. recordDeckLoadTiming already lives
 * outside it for that reason and this follows the same seam. The engine's
 * whole share of this change is a rename and one import line, paid for by
 * folding its two-line stopwatch into `beginDeckLoad`.
 *
 * WHY IT IS NOT IN perf-event-log.ts. That module has NO IMPORTS and says so
 * as a hard property, for a reason its own header records at length. The
 * dependency runs one way only: this module imports it, never the reverse.
 *
 * WHAT IT COSTS THE LOAD PATH. One object push at `beginDeckLoad`, one
 * id-bound lookup plus a bounded-array trim at the row write, and a sweep
 * over at most MAX_TRACKED_SPANS-or-more spans (see MAX_TRACKED_SPANS) plus
 * one read of an already-cached snapshot. No await, no fetch, no clock
 * beyond the `performance.now()` the load already took. The pressure
 * reading is fetched by a poller on its own timer and is only ever READ
 * here.
 *
 * WHY THE FAILURE REPORT LIVES HERE TOO. It arrived first, as
 * deck-load-failure-context.ts, under the same convention (D5, docs/perf/
 * performance-register.md: perf instrumentation belongs in a DRY module and
 * the fat file gets a call site, not a formula) and with the same single
 * caller. Two modules answering "what does the engine call out to about a
 * deck load" is one dependency more than the engine can carry: it sits at the
 * ratchet's frontend.max_fan_out ceiling as well as its size ceiling, so a
 * second door here would have reddened the gate for every other lane. One
 * module, one import, both halves of a load's telemetry.
 */

import { ApiError } from '$lib/api/errors';
import { reportClientError, type ClientErrorContext } from '$lib/client-error-reporting';
import type { DeckLoadOptions } from '$lib/rb/audio-engine-types';
import { concurrencyLabels, type DeckLoadSpan } from '$lib/rb/deck-load-concurrency';
import { pressureLabels, readMachinePressure } from '$lib/rb/machine-pressure';
import { recordDeckLoadTiming, recordPerfEvent, type StemLoadFacts } from '$lib/rb/perf-event-log';
import { pushToast } from '$lib/stores.svelte';

/**
 * How many recently COMPLETED load spans stay resident.
 *
 * 16 matches the ring's own DECK_LOAD_BUDGET (four full 4-deck loads), which
 * is the most rows a reader can compare anyway. The bound applies only to
 * completed spans: an in-flight one is never evicted, however long the
 * history grows around it, because dropping it mid-load is exactly the
 * defect this cap once had (#1658 review) -- a still-running load that later
 * rows cannot see reports `solo` wrong while looking like a real
 * measurement. Trimming happens when a span CLOSES rather than when one
 * OPENS, since opening never changes the completed count.
 */
const MAX_TRACKED_SPANS = 16;

interface MutableSpan {
	id: number;
	deck: 1 | 2 | 3 | 4 | null;
	startMs: number;
	endMs: number | null;
}

let _spans: MutableSpan[] = [];
let _nextSpanId = 1;

/** Elapsed milliseconds since this load began. */
export type DeckLoadClock = () => number;

/**
 * A stopwatch bound to the exact span `beginDeckLoad` opened for this call.
 *
 * `spanId` is what lets `recordDeckLoad` close THIS invocation's span rather
 * than guessing from deck plus recency -- see `beginDeckLoad`.
 */
export interface DeckLoadHandle {
	readonly clock: DeckLoadClock;
	readonly spanId: number;
}

/**
 * Open a span for a load that is starting, and hand back its stopwatch bound
 * to that span's identity.
 *
 * The span is registered while the load is IN FLIGHT, not reconstructed
 * afterwards from its duration, and that is the whole correctness argument.
 * Reconstructing at write time would let the load that finishes FIRST claim
 * `solo=1`: the load it was racing has not written its row yet, so it would
 * be invisible. An in-flight span is visible to every row written during it.
 *
 * The id travels with the handle rather than being re-derived from deck at
 * close time, because deck alone cannot tell two concurrent loads on the SAME
 * deck apart: `performance-controls.spec.ts` starts two `engine.load(4, ...)`
 * calls at once, and whichever one finishes first must close its OWN span,
 * not whichever same-deck span happens to still be open (#1658 review).
 */
export function beginDeckLoad(deck: 1 | 2 | 3 | 4 | null): DeckLoadHandle {
	const startMs = performance.now();
	const span: MutableSpan = { id: _nextSpanId++, deck, startMs, endMs: null };
	_spans.push(span);
	return { clock: () => Math.round(performance.now() - startMs), spanId: span.id };
}

/**
 * Drop completed spans past MAX_TRACKED_SPANS, most recent first. In-flight
 * spans (`endMs === null`) are never counted or removed here.
 *
 * A completed span past the cap survives anyway if some still-open span
 * could have overlapped it (`open.startMs <= candidate.endMs`): that open
 * span has not written its row yet, and its eventual close is the read that
 * needs this span in `_spans` to see the overlap at all. Evicting on the cap
 * alone reintroduces Thread B one call later -- not at push time, but at
 * whichever close pushes the completed count past 16 while a genuine witness
 * is still running (#1658 review follow-up).
 */
function _trimCompletedSpans(): void {
	let completedSeen = 0;
	for (let i = _spans.length - 1; i >= 0; i--) {
		const candidate = _spans[i];
		if (candidate.endMs === null) continue;
		completedSeen++;
		if (completedSeen <= MAX_TRACKED_SPANS) continue;
		const stillWatched = _spans.some(
			(other) => other.endMs === null && other.startMs <= candidate.endMs!
		);
		if (!stillWatched) _spans.splice(i, 1);
	}
}

/**
 * The span this row belongs to, closed at `nowMs`.
 *
 * Looked up by `spanId`, not by deck plus recency: the caller's own
 * `beginDeckLoad` handle says exactly which span this row closes, so two
 * loads racing on the same deck each close their own interval regardless of
 * finish order (#1658 review).
 *
 * A row whose `spanId` cannot be found in-flight gets a span synthesized
 * from the row's own measured duration instead. Once every `load()` exit
 * path closes the span it opened (true on this branch) and in-flight spans
 * are immune to the history trim (also true on this branch), a live span
 * should always be found; this branch exists as a fail-safe for a stale or
 * already-closed id rather than a path normal operation is expected to take.
 * That is honest for the overlap test -- the interval is the one the load
 * really occupied -- and it is the only case where the id is not one this
 * module issued, which is why the synthesized id is 0 and can never collide
 * with a live span.
 */
function _closeSpan(
	deck: 1 | 2 | 3 | 4 | null,
	spanId: number,
	durationMs: number,
	nowMs: number
): DeckLoadSpan {
	const span = _spans.find((candidate) => candidate.id === spanId && candidate.endMs === null);
	if (span !== undefined) {
		// Trim BEFORE marking this span closed, while it still reads as
		// in-flight, so it cannot count as its own 17th completed entry and
		// evict itself on the way out -- a still-open witness that closes
		// later would otherwise find no trace of a span that genuinely
		// overlapped it (#1658 review follow-up).
		_trimCompletedSpans();
		span.endMs = nowMs;
		return { id: span.id, deck: span.deck, startMs: span.startMs, endMs: nowMs };
	}
	return { id: 0, deck, startMs: nowMs - durationMs, endMs: nowMs };
}

/**
 * The load's own wall time, in the `performance.now()` domain.
 *
 * `total` is the successful load's full duration and `failedAt` the failed
 * one's. Neither is guaranteed present on a caller-supplied stage map, so a
 * missing one falls back to 0, which makes the synthesized-span path above
 * degenerate to a single instant rather than to an interval running from the
 * epoch and overlapping everything.
 */
function _durationMsOf(stages: Record<string, number>): number {
	const measured = stages.total ?? stages.failedAt;
	return typeof measured === 'number' && Number.isFinite(measured) ? measured : 0;
}

/**
 * Write one deck-load timing row carrying its measurement conditions.
 *
 * Drop-in for `recordDeckLoadTiming`: same arguments, same row, plus the
 * labels. The stem facts still come from perf-event-log, which owns that
 * derivation; this adds `solo`, `concurrent_loads` and the pressure stamp.
 *
 * `spanId` is the id `beginDeckLoad` handed back for THIS load, so the row
 * closes the span this invocation opened rather than whichever span the
 * deck's most recent still-open load happens to be (#1658 review).
 */
export function recordDeckLoad(
	kind: string,
	stages: Record<string, number>,
	deck: 1 | 2 | 3 | 4 | null,
	stems: StemLoadFacts,
	spanId: number
): void {
	const nowMs = performance.now();
	const subject = _closeSpan(deck, spanId, _durationMsOf(stages), nowMs);
	recordDeckLoadTiming(kind, stages, deck, stems, {
		...concurrencyLabels(subject, _spans, nowMs),
		...pressureLabels(readMachinePressure(), Date.now())
	});
}

// --------------------------------------------- failed loads: the report

/** Server-side cap on ClientErrorIn.context; anything past it is dropped. */
export const MAX_CONTEXT_KEYS = 32;

/** Namespace for flattened stage keys, so a stage cannot collide with
 * `source` or `deck` and make a report unattributable. */
const STAGE_PREFIX = 'stage_';

/**
 * Stages kept first when the map does not fit, most diagnostic first.
 *
 * `failedAt` says WHEN the load died and is the reason this context exists; the
 * rest are the heavy stages that explain a slow failure (a stem bundle fetch or
 * decode, a processor create) plus the payload size they scale with. A plain
 * insertion-order clamp would keep whichever stages happened to run first,
 * which on the stem path is the cheap metadata fetches.
 */
const PRIORITY_STAGES: readonly string[] = [
	'failedAt',
	'fetchWall',
	'decodeMix',
	'fetchStems',
	'decodeStems',
	'stemProcessorCreate',
	'stretchCreate',
	'audioBytes'
];

/**
 * Flatten one load's stage map into report context.
 *
 * Never throws and never rejects a value: this runs inside the load() catch
 * block, so a failure here would replace the application error it exists to
 * describe. A stage whose value is not a finite number is therefore omitted
 * rather than reported - JSON.stringify turns NaN into null, which on the wire
 * reads as "measured, value unknown" instead of "not measured".
 */
export function deckLoadFailureContext(
	deck: 1 | 2 | 3 | 4,
	stages: Readonly<Record<string, number>>
): ClientErrorContext {
	const context: ClientErrorContext = { source: 'deck-load', deck };
	const measured = Object.keys(stages).filter((name) => Number.isFinite(stages[name]));
	const ordered = [
		...PRIORITY_STAGES.filter((name) => measured.includes(name)),
		...measured.filter((name) => !PRIORITY_STAGES.includes(name))
	];
	for (const name of ordered) {
		if (Object.keys(context).length >= MAX_CONTEXT_KEYS) break;
		context[`${STAGE_PREFIX}${name}`] = stages[name];
	}
	return context;
}

/**
 * Load failures a DJ can act on, said plainly (CLOUDSYNC-33). The message
 * arrives as RbApiError's `CODE: detail`; for these codes the deck shows only
 * the sentence, while the toast's error report still carries the raw cause.
 * AUDIO_FILE_MISSING is deliberately absent: it also covers iCloud stubs,
 * unsupported extensions and unresolvable paths, where the file IS here.
 */
const PLAIN_LOAD_FAILURES: Readonly<Record<string, string>> = {
	AUDIO_NOT_ON_THIS_MACHINE: "This file isn't on this computer."
};

function plainLoadFailure(message: string): string {
	const code = /^([A-Z][A-Z0-9_]+): /.exec(message)?.[1];
	return (code !== undefined && PLAIN_LOAD_FAILURES[code]) || message;
}

export function formatDeckLoadFailureMessage(
	trackTitle: string | null | undefined,
	stableId: string,
	message: string
): string {
	const title = trackTitle?.trim();
	const reason = plainLoadFailure(message);
	return title ? `${title}: ${reason}` : `${stableId}: ${reason}`;
}

/**
 * The deck banner's text for a failed load, keyed by the error that failed it
 * (CLOUDSYNC-33). The banner is filled from the rejected command's error, which
 * only knows `RbApiError: CODE: detail`; the load path knows the title and the
 * plain wording, so it records them here against the same error object.
 * A WeakMap, so nothing stale can outlive the error or leak onto another load.
 */
const deckFacingMessages = new WeakMap<object, string>();

export function rememberDeckFacingMessage(error: unknown, message: string): void {
	if (typeof error === 'object' && error !== null) deckFacingMessages.set(error, message);
}

export function deckFacingMessage(error: unknown): string | undefined {
	return typeof error === 'object' && error !== null ? deckFacingMessages.get(error) : undefined;
}

/**
 * The track title from a load's own metadata request, for a load that failed
 * before that request was read (the audio fetch rejects first). Never throws.
 *
 * Deliberately unbounded and timer-free: the request is already in flight to a
 * server that has just answered the audio fetch, and the load-failure path
 * must not schedule stray timers (audio-engine-controller's timer guard).
 */
export async function settledTrackTitle(
	request: Promise<{ track: { title?: string | null } }> | null
): Promise<string | null> {
	if (request === null) return null;
	try {
		return (await request).track.title ?? null;
	} catch {
		return null;
	}
}

/**
 * The deck-facing text for a failed load (CLOUDSYNC-33): the title, from the
 * loaded track or else the load's own in-flight metadata request, then the
 * plain reason. Recorded against `error` so the deck banner shows the same.
 */
export async function failedDeckLoadMessage(
	error: unknown,
	title: string | null | undefined | Promise<{ track: { title?: string | null } }>,
	stableId: string,
	message: string
): Promise<string> {
	const resolved = title instanceof Promise ? await settledTrackTitle(title) : title;
	const text = formatDeckLoadFailureMessage(resolved, stableId, message);
	rememberDeckFacingMessage(error, text);
	return text;
}

/**
 * Report one failed deck load: the user-facing toast, the client perf ring, and
 * - riding that same toast - the server-side error row carrying the stages.
 *
 * The stages are the only record of WHERE the load died, and until this existed
 * they stopped at the client ring, which the next fader drag wipes. Riding the
 * toast's own error report is what carries them to the server WITHOUT a second
 * reportClientError per failure: client-error-reporting dedupes on `source`, so
 * two reports per failure would land as two rows with the diagnosis on neither.
 *
 * Ordering is load-bearing and the caller owns it: `stages.failedAt` must
 * already be stamped when this is called, or every report is missing the one
 * number that says when the load died.
 */
/** Plain words for a stick load refusal, keyed on the backend's detail.code
 * (spec 4b, USBPLAY-09). */
const STICK_LOAD_FAILURE_WORDS: ReadonlyMap<string, string> = new Map([
	['USB_STICK_NOT_MOUNTED', 'Stick removed - plug it back in to load this track'],
	['USB_FILE_MISSING', "This track's audio file is missing from the stick"],
	['USB_TRACK_NOT_FOUND', "This track is no longer in the stick's rekordbox export"]
]);

/** The toast headline for a failed stick load, or null when `cause` is not a
 * stick refusal. Reads `code` off either error class a load can reject with
 * (ApiError from getTrack, RbApiError from audio, anlz and hot cues). */
export function stickLoadFailureWords(cause: unknown): string | null {
	const code = typeof cause === 'object' && cause !== null ? (cause as { code?: unknown }).code : null;
	return typeof code === 'string' ? (STICK_LOAD_FAILURE_WORDS.get(code) ?? null) : null;
}

/** Plain words for a library load refusal, keyed on the backend's detail.code
 * (apps/adapters/rekordbox/paths.py resolve_playable_audio, and the audio
 * route's open probe). The reason goes in the HEADLINE (pin a4898e22): a
 * generic "could not load the track" made the operator expand the toast to
 * learn the file was simply missing. */
const LIBRARY_LOAD_FAILURE_WORDS: ReadonlyMap<string, string> = new Map([
	['TRACK_NOT_FOUND', 'this track is no longer in the library'],
	['AUDIO_FILE_MISSING', "this track's audio file is missing on this machine"],
	['CLOUD_ASSET_UNAVAILABLE', "this track's audio is not on this machine and could not be fetched"],
	['CLOUD_POLICY_UNCONFIGURED', 'cloud audio is not set up on this machine, so this track cannot be fetched'],
	['AUDIO_ACCESS_BLOCKED', "this track's file did not open in time - its drive or folder is not answering"]
]);

/** What fetch rejects with when nothing answered. The text is the browser's
 * own and differs per engine (Chromium, WebKit, Gecko). */
const UNREACHABLE_FETCH_MESSAGES: ReadonlySet<string> = new Set([
	'Failed to fetch',
	'Load failed',
	'NetworkError when attempting to fetch resource.'
]);

const HEADLINE_REASON_MAX_CHARS = 120;

function _shortReason(cause: unknown): string {
	const text = cause instanceof Error ? cause.message : String(cause);
	const firstLine = text.split('\n', 1)[0].trim();
	return firstLine.length <= HEADLINE_REASON_MAX_CHARS
		? firstLine
		: `${firstLine.slice(0, HEADLINE_REASON_MAX_CHARS - 3)}...`;
}

/**
 * The track route answers an unknown stable_id with a bare 404 (`errors.py`
 * handle_not_found carries no detail.code), so getTrack rejects with
 * HTTP_404 while the audio route rejects the same load with TRACK_NOT_FOUND.
 * The load fetches both at once and whichever rejects first names the toast,
 * so the headline flipped between "no longer in the library" and "could not
 * load the track: Not Found" by timing alone. Give the lookup's 404 the same
 * code. A stick lookup's 404 carries USB_TRACK_NOT_FOUND and is left alone.
 */
export function libraryTrackLookupError(error: unknown): unknown {
	if (error instanceof ApiError && error.status === 404 && error.code === 'HTTP_404') {
		return new ApiError(404, 'TRACK_NOT_FOUND', error.message, error.response, error.body);
	}
	return error;
}

/** `libraryTrackLookupError` as a `.catch` handler for the load's getTrack. */
export function rethrowLibraryTrackLookupError(error: unknown): never {
	throw libraryTrackLookupError(error);
}

/** The toast headline for a failed deck load: always names the reason. */
export function deckLoadFailureHeadline(deck: 1 | 2 | 3 | 4, cause: unknown): string {
	const stickWords = stickLoadFailureWords(cause);
	if (stickWords !== null) return stickWords;
	const code = typeof cause === 'object' && cause !== null ? (cause as { code?: unknown }).code : null;
	const words = typeof code === 'string' ? LIBRARY_LOAD_FAILURE_WORDS.get(code) : undefined;
	if (words !== undefined) return `Deck ${deck}: ${words}`;
	if (cause instanceof Error && cause.name === 'EncodingError') {
		return `Deck ${deck}: this track's audio file could not be decoded`;
	}
	if (cause instanceof TypeError && UNREACHABLE_FETCH_MESSAGES.has(cause.message)) {
		return `Deck ${deck}: the engine did not answer while loading this track`;
	}
	return `Deck ${deck} could not load the track: ${_shortReason(cause)}`;
}

/** Failures the engine has already put on screen. The same error object then
 * rejects the load COMMAND, whose dispatcher reports failures too: without
 * this one failed load raised two toasts. Weak, so a reported error is not
 * kept alive; a thrown non-object cannot be tracked and is reported twice. */
const _toastedLoadFailures = new WeakSet<object>();

function _pushDeckLoadFailureToast(
	deck: 1 | 2 | 3 | 4,
	message: string,
	cause: unknown,
	context: ClientErrorContext,
	groupKey?: string
): void {
	pushToast(
		`Deck ${deck} load failed - ${message}`,
		'error',
		undefined,
		cause,
		context,
		groupKey,
		undefined,
		{ headline: deckLoadFailureHeadline(deck, cause) }
	);
	if (typeof cause === 'object' && cause !== null) _toastedLoadFailures.add(cause);
}

export function reportDeckLoadFailure(
	deck: 1 | 2 | 3 | 4,
	message: string,
	cause: unknown,
	stages: Readonly<Record<string, number>>,
	options: DeckLoadOptions
): void {
	const failureContext = deckLoadFailureContext(deck, stages);
	// A load whose caller shows its own toast (Trackify, #4036) still owes the
	// server this report: it is the only record of which stage the load died in.
	if (options.suppressFailureToast === true) {
		reportClientError(cause, failureContext);
	} else {
		_pushDeckLoadFailureToast(deck, message, cause, failureContext);
	}
	recordPerfEvent('deck-load-fail', message, deck);
}

/**
 * The load COMMAND failed. Called by the command dispatcher for every rejected
 * load. A failure the engine already toasted is left alone; a load refused
 * before it reached the engine (another owner holds the controls, the deck is
 * not stopped) is reported here, as a load, with its reason.
 */
export function reportDeckLoadCommandFailure(deck: 1 | 2 | 3 | 4, message: string, cause: unknown): void {
	if (typeof cause === 'object' && cause !== null && _toastedLoadFailures.has(cause)) return;
	// Grouped per deck, as the dispatcher groups every other command failure:
	// a refusal repeated by a held key is one toast with a count, not a stack.
	_pushDeckLoadFailureToast(deck, message, cause, { source: 'deck-load', deck }, `performance:${deck}:load:`);
}
