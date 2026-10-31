/**
 * CMDK-20..22 (issue #6093): Tab for AI search in the Cmd K bar, runeless so the
 * unit suite loads it.
 *
 * Two steps. First the bar shortlists tracks that can mix into the master from
 * its own in-memory index (`shortlist`): a tempo the master can reach, a key
 * next to the master's on the Camelot wheel, not already played. Then the
 * engine asks JEV ONCE whether each fits the DJ's words
 * (`POST /api/v1/cmdk/ai-search`), and `rankByFit` orders the shortlist by
 * that probability. The chip on each row comes from the track's own data; the
 * model never sees audio and its answer never changes a row's BPM or key.
 */
import { camelotKeysWithinSteps, effectiveCamelotKey } from '$lib/player/key/camelot';

/** About this many go to JEV: enough to choose from, small enough for one call. */
export const SHORTLIST_SIZE = 50;
/** A tempo within this share of the master's (or of half or double it) can be mixed in. */
export const BPM_TOLERANCE = 0.06;
/** Steps round the Camelot wheel that still mix (the same number, or one either side). */
export const KEY_STEPS = 1;
/** The DJ's words need at least this many characters before a call is made. */
export const AI_MIN_CHARS = 3;
/** Typing pauses this long before the call goes out, so a call is not made per keystroke. */
export const AI_DEBOUNCE_MS = 450;

export type CmdkMode = 'plain' | 'ai';

export interface AiRow {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album?: string | null | undefined;
	genre?: string | null | undefined;
	bpm: number | null;
	key: string | null;
}

export interface MasterTrack {
	bpm: number | null;
	key: string | null;
}

/**
 * The master as it sounds, not as it was tagged: the tag BPM times the tempo
 * fader, and the key after its manual shift (the key the deck header shows).
 * Not `deckEffectiveBpm`: that follows the beatgrid at the playhead, so the
 * shortlist would re-derive every frame while the master plays.
 */
export function audibleMaster(deck: { bpm: number | null; pitch: number; key: string | null; key_shift_semitones: number }): MasterTrack {
	return {
		bpm: deck.bpm === null ? null : deck.bpm * deck.pitch,
		key: effectiveCamelotKey(deck.key, deck.key_shift_semitones)
	};
}

/** Whether `bpm` is within reach of the master's tempo, at the same, half or double time. */
export function tempoFits(bpm: number | null, master: number | null): boolean {
	if (master === null || !(master > 0)) return true;
	if (bpm === null || !(bpm > 0)) return false;
	return [master, master / 2, master * 2].some((m) => Math.abs(bpm - m) <= m * BPM_TOLERANCE);
}

/** Whether `key` mixes with the master's. A master with no key lets every key through. */
export function keyFits(key: string | null, master: string | null): boolean {
	if (master === null || master.trim() === '') return true;
	return camelotKeysWithinSteps(key, master, KEY_STEPS);
}

/**
 * Up to `SHORTLIST_SIZE` rows that can mix into the master and were not
 * played, in index order, each once. With no master track every unplayed row
 * qualifies (there is nothing to mix into yet).
 */
export function shortlist<T extends AiRow>(
	rows: readonly T[],
	master: MasterTrack | null,
	played: ReadonlySet<string>,
	size = SHORTLIST_SIZE
): T[] {
	const out: T[] = [];
	const seen = new Set<string>();
	for (const row of rows) {
		if (out.length >= size) break;
		if (seen.has(row.stable_id) || played.has(row.stable_id)) continue;
		if (master !== null && (!tempoFits(row.bpm, master.bpm) || !keyFits(row.key, master.key))) continue;
		seen.add(row.stable_id);
		out.push(row);
	}
	return out;
}

/** What the engine is sent for one row: its text fields only. */
export function aiTrack(row: AiRow): {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album: string | null;
	genre: string | null;
	bpm: number | null;
	key: string | null;
} {
	return {
		stable_id: row.stable_id,
		title: row.title,
		artist: row.artist,
		album: row.album ?? null,
		genre: row.genre ?? null,
		bpm: row.bpm,
		key: row.key
	};
}

/**
 * The shortlist ordered by JEV's probability, best first, each with its
 * probability. A row JEV gave no number for is left out rather than guessed;
 * ties keep the shortlist's order.
 */
export function rankByFit<T extends { stable_id: string }>(
	rows: readonly T[],
	scores: Readonly<Record<string, number>>
): Array<{ row: T; p: number }> {
	return rows
		.map((row, i) => ({ row, p: scores[row.stable_id], i }))
		.filter((x): x is { row: T; p: number; i: number } => typeof x.p === 'number' && Number.isFinite(x.p))
		.sort((a, b) => b.p - a.p || a.i - b.i)
		.map(({ row, p }) => ({ row, p }));
}

/** Tab flips the mode; any other key leaves it. The typed words are kept either way. */
export function nextMode(mode: CmdkMode, key: string, modifiers: { shift: boolean; meta: boolean; ctrl: boolean; alt: boolean }): CmdkMode | null {
	if (key !== 'Tab' || modifiers.shift || modifiers.meta || modifiers.ctrl || modifiers.alt) return null;
	return mode === 'plain' ? 'ai' : 'plain';
}

/** The probability as the row shows it, e.g. "77%". */
export function fitLabel(p: number): string {
	return `${Math.round(p * 100)}%`;
}
