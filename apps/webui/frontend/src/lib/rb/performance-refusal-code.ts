/**
 * #12280: a refused performance command carries a stable, machine-readable
 * code next to its human toast text, so an agent reading the command status
 * (`queryPerformanceState().last_error_code`, `decks[n].command_error_code`,
 * and the same fields in the UI mirror) never has to parse prose.
 *
 * The vocabulary is the one refusals already speak, not a parallel one:
 *   - an error that carries its own string `code` keeps it. The Rust engine's
 *     `EngineCommandError` (`apps/audio-engine/src/engine.rs` `ErrorCode`:
 *     invalid, unsupported, not_implemented, no_track, no_beatgrid, decode,
 *     io, wrong_clock), `DeckReplacementBlockedError` (deck_playing) and
 *     `DeckMoveRefused` all do.
 *   - the page's own uncoded refusals map onto the engine code that means the
 *     same thing: an empty-deck play or cue is `no_track`, a command the Rust
 *     mode has not built yet is `not_implemented`.
 *   - anything else is the explicit `unknown`; its reason text stays in
 *     `last_error` / `command_error` untouched.
 *
 * Codes are kept by message text, the key the command status already stores,
 * so every place that clears `last_error` (a later command starting, a
 * successful one, a dismissed deck banner) clears the code with it: a code is
 * only ever read back next to the text it was recorded for.
 */

/** The code for a refusal that names no reason this module knows. */
export const UNKNOWN_REFUSAL_CODE = 'unknown';

/** How many distinct refusal texts the book remembers (4 decks plus global, with headroom). */
export const REFUSAL_CODE_BOOK_CAPACITY = 32;

const CODE_SHAPE = /^[A-Za-z][A-Za-z0-9_]*$/;

/** Uncoded page refusals, mapped onto the engine's own code for the same case. */
const MESSAGE_CODES: readonly (readonly [RegExp, string])[] = [
	[/no track loaded on deck \d+$/, 'no_track'],
	[/ is empty - load a track before /, 'no_track'],
	[/RUST_ENGINE_UNAVAILABLE: /, 'not_implemented']
];

/** The stable code a refused command's error carries, or `unknown`. */
export function refusalCodeOf(error: unknown): string {
	const own =
		typeof error === 'object' && error !== null ? (error as { code?: unknown }).code : undefined;
	if (typeof own === 'string' && CODE_SHAPE.test(own)) return own;
	const text = error instanceof Error ? error.message : String(error);
	for (const [pattern, code] of MESSAGE_CODES) {
		if (pattern.test(text)) return code;
	}
	return UNKNOWN_REFUSAL_CODE;
}

export interface RefusalCodeBook {
	/** Record the code for the status text a refusal was persisted under. */
	note(message: string, error: unknown): void;
	/** The code for a status text: null when there is no error, `unknown` when none was noted. */
	codeFor(message: string | null): string | null;
}

/** A bounded message-to-code book; the oldest text is forgotten first. */
export function createRefusalCodeBook(capacity: number): RefusalCodeBook {
	if (!Number.isInteger(capacity) || capacity < 1) {
		throw new RangeError(`refusal code book capacity must be a positive integer, got ${capacity}`);
	}
	const codes = new Map<string, string>();
	return {
		note(message, error) {
			codes.delete(message);
			codes.set(message, refusalCodeOf(error));
			if (codes.size > capacity) codes.delete(codes.keys().next().value as string);
		},
		codeFor(message) {
			if (message === null) return null;
			return codes.get(message) ?? UNKNOWN_REFUSAL_CODE;
		}
	};
}

/** The book `performance-ipc` writes on every persisted command error. */
export const performanceRefusalCodes: RefusalCodeBook = createRefusalCodeBook(
	REFUSAL_CODE_BOOK_CAPACITY
);
