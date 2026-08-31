/**
 * The stage map of a FAILED deck load, shaped for the server error report.
 *
 * A failed load already measures where it died - the catch block in
 * audio-engine's load() stamps `failedAt` onto the same stage map it has been
 * filling all the way down - but that map only ever reached the client-side
 * perf ring. The toast raised next is what travels to the server-side
 * webui-client-errors JSONL, so the server learned "deck 2 failed with
 * AUDIO_FILE_MISSING" and never learned whether that took 40ms in getTrack or
 * nine seconds in decodeStems.
 *
 * ClientErrorIn.context on the server already accepts
 * dict[str, str|int|float|bool|None] with at most 32 keys, so the stage map
 * travels as flattened context and no schema changes.
 *
 * The whole reporting step lives here rather than in audio-engine (convention
 * D5, docs/perf/performance-register.md): perf instrumentation belongs in a
 * DRY module and the fat file gets a call site, not a formula.
 */

import { recordPerfEvent } from '$lib/rb/perf-event-log';
import type { ClientErrorContext } from '$lib/client-error-reporting';
import { pushToast } from '$lib/stores.svelte';

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
export function reportDeckLoadFailure(
	deck: 1 | 2 | 3 | 4,
	message: string,
	cause: unknown,
	stages: Readonly<Record<string, number>>
): void {
	const failureContext = deckLoadFailureContext(deck, stages);
	pushToast(`Deck ${deck} load failed - ${message}`, 'error', undefined, cause, failureContext);
	recordPerfEvent('deck-load-fail', message, deck);
}
