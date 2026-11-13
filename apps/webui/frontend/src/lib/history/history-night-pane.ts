/**
 * One History night in the track table, with a Played at column (PLAYS-9745 remainder).
 *
 * rekordbox History (mimicked): opening a night lists its tracks in the track
 * list in play order with the time each was played. A night is addressed as the
 * pane id `history:YYYY-MM-DD`, so the browser sort, the nav back-stack and the
 * `browser_select_playlist` performance command reach it like any other list.
 * Pure: no Svelte, no DOM.
 */
import type { PlaylistNode } from '$lib/rb/library-types';
import type { HistoryPlay } from './history-api';
import { formatPlayedAt, historyNightLabel } from './history-view';

const NIGHT_ID = /^history:(\d{4}-\d{2}-\d{2})$/;

/** The pane id that opens one night in the track table: `history:2026-11-13`. */
export function historyNightId(date: string): string {
	const id = `history:${date}`;
	if (!NIGHT_ID.test(id))
		throw new Error(`history: ${JSON.stringify(date)} is not a YYYY-MM-DD night`);
	return id;
}

/** The night a pane id addresses, or null when the id is not a History night. */
export function historyNightDate(playlistId: string | null): string | null {
	return NIGHT_ID.exec(playlistId ?? '')?.[1] ?? null;
}

/** The read-only list node that loads one night into a pane. */
export function historyNightNode(date: string, trackCount = 0): PlaylistNode {
	return {
		playlist_id: historyNightId(date),
		name: `History ${historyNightLabel(date)}`,
		track_count: trackCount,
		broken_count: 0,
		kind: 'history',
		children: []
	};
}

/** One table row per play whose track is still in the collection, in play order, each carrying its played_at. */
export function historyNightRows<Item extends { stable_id: string }, Row extends object>(
	plays: readonly HistoryPlay[],
	items: readonly Item[],
	toRow: (item: Item, order: number) => Row
): (Row & { played_at: string })[] {
	const byId = new Map(items.map((item) => [item.stable_id, item]));
	const rows: (Row & { played_at: string })[] = [];
	for (const play of plays) {
		const item = byId.get(play.stable_id);
		if (item === undefined) continue;
		rows.push({ ...toRow(item, rows.length + 1), played_at: play.played_at });
	}
	return rows;
}

/** The Played at cell: the local HH:MM of the play, empty for a row that is not a play. */
export function playedAtCellText(row: { played_at?: string | null }, timeZone?: string): string {
	return row.played_at === null || row.played_at === undefined
		? ''
		: formatPlayedAt(row.played_at, timeZone);
}

/** True when a pane's rows are plays, so the table shows its Played at column. */
export function rowsArePlays(rows: readonly { played_at?: string | null }[]): boolean {
	return rows.some((row) => row.played_at !== null && row.played_at !== undefined);
}
