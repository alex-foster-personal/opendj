/**
 * Pin a00900579b7b. How many song rows the library table can show (the
 * viewport, not how many tracks the playlist has). Above 20, the mixer
 * draws artwork and a duplicate play button above each channel column.
 *
 * The deck row grows by MIXER_COLUMN_ART_PX when that chrome is on, which
 * shortens the table. Hiding only well under 20 stops that shrink from
 * turning the chrome off and on again.
 *
 * TOPVIEW-01: a view switch never resizes the deck row. List view hands
 * the waveform row's height to the library, so the rows are counted as in
 * waveform view: the /performance page reports the height of the collapsed
 * waveform row and it is taken off the table viewport before counting.
 * The count settles in a timer task after its inputs change, because the
 * table and the page measure through separate ResizeObservers and one can
 * report before the other within the same rendering step; a timer queued
 * from either runs after both have reported.
 */
export const MIXER_COLUMN_ART_PX = 64;

const SHOW_WHEN_SONG_ROWS_ABOVE = 20;
const HIDE_WHEN_SONG_ROWS_AT_OR_BELOW = 12;

export const librarySongCapacity = $state({
	songs: 0,
	showMixerColumnArt: false
});

let tableViewportPx = 0;
let tableRowPx = 0;
let hiddenWaveRowPx = 0;
let settleTimer: ReturnType<typeof setTimeout> | null = null;

/** Song rows the table would have with the waveform row showing. */
export function songRowsAsInWaveformView(
	viewportPx: number,
	rowPx: number,
	hiddenPx: number
): number {
	if (!(rowPx > 0)) return 0;
	return Math.max(0, Math.floor((viewportPx - hiddenPx) / rowPx));
}

export function noteLibrarySongCapacity(songs: number): void {
	if (!Number.isFinite(songs) || songs < 0) return;
	const next = Math.floor(songs);
	librarySongCapacity.songs = next;
	if (!librarySongCapacity.showMixerColumnArt && next > SHOW_WHEN_SONG_ROWS_ABOVE) {
		librarySongCapacity.showMixerColumnArt = true;
	} else if (librarySongCapacity.showMixerColumnArt && next <= HIDE_WHEN_SONG_ROWS_AT_OR_BELOW) {
		librarySongCapacity.showMixerColumnArt = false;
	}
}

function settle(): void {
	noteLibrarySongCapacity(songRowsAsInWaveformView(tableViewportPx, tableRowPx, hiddenWaveRowPx));
}

function scheduleSettle(): void {
	if (settleTimer !== null) return;
	settleTimer = setTimeout(() => {
		settleTimer = null;
		settle();
	}, 0);
}

/** The track table's scroller height below its header, and its row height. */
export function noteLibraryViewport(viewportPx: number, rowPx: number): void {
	if (!Number.isFinite(viewportPx) || !Number.isFinite(rowPx)) return;
	tableViewportPx = Math.max(0, viewportPx);
	tableRowPx = rowPx;
	scheduleSettle();
}

/** Height of the waveform row the current view collapses (0 in waveform view). */
export function noteHiddenWaveRowPx(px: number): void {
	if (!Number.isFinite(px) || px < 0) return;
	hiddenWaveRowPx = px;
	scheduleSettle();
}
