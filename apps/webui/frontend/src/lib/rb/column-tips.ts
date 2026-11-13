/**
 * Hover `title` copy for library track-table column headers.
 * Keep tips here so TrackTable / future rich tips share one source.
 */

export type LibraryColTipId =
	| 'order'
	| 'preview'
	| 'art'
	| 'title'
	| 'artist'
	| 'album'
	| 'key'
	| 'bpm'
	| 'plays'
	| 'rating'
	| 'comments'
	| 'time'
	| 'energy'
	| 'genre'
	| 'filename'
	| 'location'
	| 'date_added'
	| 'bitrate'
	| 'kind'
	| 'last_played'
	| 'played_at'
	| 'lyrics';

/** Header explainers (plain text for native `title` tooltips). */
export const COLUMN_TIPS: Record<LibraryColTipId, string> = {
	order:
		'Playlist position (#). Drag the grip when reorder is enabled to move membership order. Right-click the header on an editable playlist for Shuffle (Open DJ play counts weight recently played tracks lower).',
	preview:
		'Preview strip: tri-band waveform (low / mid / high energy). Blue vocal bars overlay when vocals are analyzed (Rekordbox PVDI or local demucs). Hover for a red scrub line; click to seek on a loaded deck. Small chevron (▸) top-left of the row = audio prefetched for fast deck load; orange dot = prefetch in progress.',
	art: 'Artist graphic. Cover / sleeve art from the library; column width also sets row height.',
	title:
		'Track title. Yellow = loaded on a non-master deck; gold = master. Double-click title/artist/art/preview to load+play.',
	artist: 'Artist name. Play counts across DJ software and USB sticks are tracked (see ▶ column).',
	album:
		'Album title from the track tags or rekordbox. Empty when the track has no album; those rows sort last.',
	key: 'Musical key (Camelot when available). Colored to match the wheel; bordered when compatible with the master key.',
	bpm: 'Tempo in BPM. Heat color vs master: green sweet spot, purple half/double lane, dim when far.',
	plays: 'Play count aggregated from DJ history (Rekordbox, djay, USB sticks where available).',
	rating: 'Star rating (1-5). Click to edit; writes back with optimistic concurrency.',
	comments: 'Free-text comments from the library record.',
	time: 'Track duration (mm:ss).',
	energy:
		'Energy 1-9, imported from Mixed In Key. Empty means no readable Mixed In Key value is available; hover the cell for the reason.',
	genre:
		'Genres prioritize Rekordbox. Agents may enrich during enrich. User can edit the enrich prompt and choose Rbx vs Mixed In Key vs OpenDJ enriched (incl. version history) in config - WIP unfinished. Click a tag to filter; double = loose; triple = undo.',
	filename: 'Audio file name from the stored path (last path segment).',
	location:
		'Folder containing the audio file (parent path without the file name). Alt+click reveals that folder in the file manager.',
	date_added:
		'Day the track entered your library (YYYY-MM-DD), the earlier of Open DJ and rekordbox. First click sorts newest first.',
	bitrate:
		'Effective bitrate in kbps (file size over duration), the same figure the Quality badge reads. Empty when it could not be measured; those rows sort last.',
	kind: 'File type from the audio file extension (MP3, FLAC, WAV, AIFF, M4A). Sorts A to Z.',
	last_played:
		'Day of the last play (YYYY-MM-DD), the later of the imported history and Open DJ plays. Empty when never played. First click sorts newest first.',
	played_at:
		'Local time (HH:MM) the track was played that night. Shown only for a History night; sorts in play order.',
	lyrics:
		'Lyrics verdict glyph and witness sync percent. Hover for the licensed lyric tip when word timings are cached. Sort by effective verdict then coverage.'
};

/** Tip + optional sort suffix for a header `title`. */
export function columnHeaderTitle(col: LibraryColTipId, sortHint?: string | null): string {
	const tip = COLUMN_TIPS[col];
	if (sortHint === undefined || sortHint === null || sortHint === '') return tip;
	return `${tip} ${sortHint}`;
}
