/**
 * Cues tab of the HOT CUE dropdown (DECKUX-9981, rekordbox CUE/LOOP list,
 * mimicked): one row per stored memory cue, hot cue and loop of the loaded
 * track, sorted by time. Pure: PadModeMenu.svelte renders the rows and
 * dispatches the command `cueRowCommand` builds.
 *
 * Loop lengths come from the actual PQTZ grid, never from the vendor
 * `beat_loop_size`, which djmdCue can store packed.
 */
import type { AnlzBeat, AnlzCue } from './anlz-types';
import type { DeckId } from './deck-id';
import type { HotCue } from './hot-cue-types';
import { beatCoordinate } from './rescue-beat-stamp';

/** The cue fields a row reads; `beat_loop_size` is deliberately absent. */
export type CueListSource = Pick<
	AnlzCue,
	'kind' | 'slot' | 'in_ms' | 'out_ms' | 'is_loop' | 'color_table_index' | 'comment'
>;

/** The kinds the list shows; `transition_in` is not a stored cue row. */
export type CueListKind = 'memory' | 'hot_cue' | 'loop';

export interface CueListRow {
	/** Stable key for keyed rendering. */
	key: string;
	kind: CueListKind;
	slot: AnlzCue['slot'];
	in_ms: number;
	out_ms: number | null;
	/** True when the row engages a loop (both endpoints stored). */
	isLoop: boolean;
	/** Cue-in time as mm:ss.mmm. */
	time: string;
	/** Loop length in beats on the PQTZ grid; null for a point cue or off-grid loop. */
	loopBeats: number | null;
	comment: string | null;
	color_table_index: number | null;
}

/** Commands a row click dispatches (a subset of PerformanceCommand). */
export type CueRowCommand =
	| { type: 'seek'; deck: DeckId; position_ms: number; exact: true }
	| { type: 'loop'; deck: DeckId; loop: { in_ms: number; out_ms: number }; quantize: false };

const KIND_ORDER: Record<CueListKind, number> = { memory: 0, loop: 1, hot_cue: 2 };

/** Format a cue time in ms as mm:ss.mmm, the rekordbox list format. */
export function formatCueListTime(ms: number): string {
	const whole = Math.max(0, Math.round(ms));
	const seconds = Math.floor(whole / 1000);
	const minutes = String(Math.floor(seconds / 60)).padStart(2, '0');
	return `${minutes}:${String(seconds % 60).padStart(2, '0')}.${String(whole % 1000).padStart(3, '0')}`;
}

/** Loop length in beats between two times on the PQTZ grid, to 2 decimals. */
export function loopBeatsOnGrid(
	beats: readonly Pick<AnlzBeat, 't'>[],
	inMs: number,
	outMs: number
): number | null {
	if (!Number.isFinite(inMs) || !Number.isFinite(outMs) || outMs <= inMs) return null;
	const start = beatCoordinate(beats, inMs / 1000);
	const end = beatCoordinate(beats, outMs / 1000);
	if (start === null || end === null || end <= start) return null;
	return Math.round((end - start) * 100) / 100;
}

/** Rows sorted by `in_ms` (memory, then loop, then hot cue on a tie). */
export function cueListRows(
	cues: readonly CueListSource[],
	beats: readonly Pick<AnlzBeat, 't'>[]
): CueListRow[] {
	const rows: CueListRow[] = [];
	for (const cue of cues) {
		if (cue.kind === 'transition_in' || !Number.isFinite(cue.in_ms)) continue;
		const isLoop = cue.is_loop && cue.out_ms !== null && cue.out_ms > cue.in_ms;
		rows.push({
			key: `${cue.kind}:${cue.slot ?? ''}:${cue.in_ms}`,
			kind: cue.kind,
			slot: cue.slot,
			in_ms: cue.in_ms,
			out_ms: isLoop ? cue.out_ms : null,
			isLoop,
			time: formatCueListTime(cue.in_ms),
			loopBeats:
				isLoop && cue.out_ms !== null ? loopBeatsOnGrid(beats, cue.in_ms, cue.out_ms) : null,
			comment: cue.comment,
			color_table_index: cue.color_table_index
		});
	}
	return rows.sort(
		(a, b) =>
			a.in_ms - b.in_ms ||
			KIND_ORDER[a.kind] - KIND_ORDER[b.kind] ||
			(a.slot ?? '').localeCompare(b.slot ?? '')
	);
}

/** The deck's list sources: stored memory cues and loops from ANLZ plus the
 * live hot-cue bank, so a hot cue saved or cleared this session shows at once. */
export function deckCueListSources(
	anlzCues: readonly CueListSource[],
	hotCues: readonly HotCue[]
): CueListSource[] {
	const stored = anlzCues.filter((cue) => cue.kind === 'memory' || cue.kind === 'loop');
	const hot = hotCues.map((cue) => ({
		kind: 'hot_cue' as const,
		slot: cue.slot,
		in_ms: cue.in_ms,
		out_ms: cue.out_ms,
		is_loop: cue.is_loop,
		color_table_index: cue.color_table_index,
		comment: cue.comment
	}));
	return [...stored, ...hot];
}

/** A loop row engages its loop exactly; any other row seeks exactly (no quantize). */
export function cueRowCommand(deck: DeckId, row: CueListRow): CueRowCommand {
	if (row.isLoop && row.out_ms !== null) {
		return { type: 'loop', deck, loop: { in_ms: row.in_ms, out_ms: row.out_ms }, quantize: false };
	}
	return { type: 'seek', deck, position_ms: row.in_ms, exact: true };
}

/** Index of the row for the engaged loop (-1 when none) and of the row whose
 * cue-in is nearest the playhead (-1 for an empty list). */
export function cueListHighlights(
	rows: readonly CueListRow[],
	positionMs: number,
	loop: { in_ms: number; out_ms: number; engaged: boolean } | null
): { engagedIndex: number; nearestIndex: number } {
	const engagedIndex =
		loop !== null && loop.engaged
			? rows.findIndex(
					(row) =>
						row.isLoop &&
						row.out_ms !== null &&
						Math.abs(row.in_ms - loop.in_ms) <= 1 &&
						Math.abs(row.out_ms - loop.out_ms) <= 1
				)
			: -1;
	let nearestIndex = -1;
	let best = Infinity;
	rows.forEach((row, index) => {
		const distance = Math.abs(row.in_ms - positionMs);
		if (distance < best) {
			best = distance;
			nearestIndex = index;
		}
	});
	return { engagedIndex, nearestIndex };
}

/** Why the Cues tab is inert, or null when a track is loaded. */
export function cueListInertReason(stableId: string | null): string | null {
	return stableId === null ? 'No track loaded - load a track to list its cues' : null;
}

/** The HOT CUE dropdown's tabs; the last one picked is kept for the session. */
export type PadMenuTab = 'modes' | 'cues';

const lastTabByDeck = new Map<DeckId, PadMenuTab>();

/** Last tab picked on this deck's dropdown this session ('modes' by default). */
export function lastPadMenuTab(deck: DeckId): PadMenuTab {
	return lastTabByDeck.get(deck) ?? 'modes';
}

/** Remember the tab picked on this deck's dropdown for the session. */
export function rememberPadMenuTab(deck: DeckId, tab: PadMenuTab): void {
	lastTabByDeck.set(deck, tab);
}
