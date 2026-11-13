/**
 * Pure shaping for the History source panel (PLAYS-9745). No Svelte, no DOM.
 *
 * rekordbox History (mimicked): one list per night named by its date, rows in
 * play order with the time each track was played.
 */
import type { HistoryDay, HistoryPlay, HistoryPlaylistSaved } from './history-api';

export interface HistoryTrackFacts {
	stable_id: string;
	title?: string | null;
	artist?: string | null;
}

export interface HistoryPlayView {
	position: number;
	stable_id: string;
	play_id: string | null;
	/** Local HH:MM the track was played. */
	time: string;
	title: string;
	artist: string;
	/** The track is no longer in the collection (Save as playlist leaves it out). */
	gone: boolean;
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** "Fri 2026-11-13": the night's date with its weekday, read as a calendar date (no zone shift). */
export function historyNightLabel(date: string): string {
	const [year, month, day] = date.split('-').map(Number);
	const weekday = WEEKDAYS[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
	return `${weekday} ${date}`;
}

/** "1 play" / "12 plays". */
export function historyCountLabel(count: number): string {
	return `${count} ${count === 1 ? 'play' : 'plays'}`;
}

/** The local HH:MM (24 h) of a canonical UTC play time; `timeZone` pins it for tests. */
export function formatPlayedAt(playedAt: string, timeZone?: string): string {
	const instant = new Date(playedAt);
	if (Number.isNaN(instant.getTime())) throw new Error(`history: unreadable played_at ${playedAt}`);
	return new Intl.DateTimeFormat('en-GB', {
		hour: '2-digit',
		minute: '2-digit',
		hour12: false,
		...(timeZone === undefined ? {} : { timeZone })
	}).format(instant);
}

/** One view row per play, titles joined from the library index; unknown ids read as gone. */
export function historyPlayRows(
	plays: readonly HistoryPlay[],
	tracks: readonly HistoryTrackFacts[],
	timeZone?: string
): HistoryPlayView[] {
	const byId = new Map(tracks.map((track) => [track.stable_id, track]));
	return plays.map((play) => {
		const track = byId.get(play.stable_id);
		return {
			position: play.position,
			stable_id: play.stable_id,
			play_id: play.play_id,
			time: formatPlayedAt(play.played_at, timeZone),
			title:
				track?.title ?? (track === undefined ? 'Track no longer in the collection' : 'Untitled'),
			artist: track?.artist ?? '',
			gone: track === undefined
		};
	});
}

/** The toast after Save as playlist: name, row count, and how many tracks were left out. */
export function historySavedMessage(saved: HistoryPlaylistSaved): string {
	const rows = `${saved.track_count} ${saved.track_count === 1 ? 'track' : 'tracks'}`;
	const skipped =
		saved.skipped.length === 0
			? ''
			: `, ${saved.skipped.length} no longer in the collection left out`;
	return `Saved playlist "${saved.name}" (${rows}${skipped})`;
}

/** The night to show after a refresh: the open one if it still holds plays, else none. */
export function keepOpenNight(open: string | null, days: readonly HistoryDay[]): string | null {
	if (open === null) return null;
	return days.some((day) => day.date === open) ? open : null;
}
