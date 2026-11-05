/**
 * TOPVIEW-05: the rows the Track information overlay shows for one track.
 * Pure: the overlay fetches the record (GET /api/v1/tracks/{id}) and the play
 * counts (GET /api/v1/tracks/{id}/plays) and renders what this returns.
 *
 * A field the record does not carry renders as "not set", never
 * as a blank cell that could read as a value that failed to draw, and each
 * row carries the provenance the engine stored for it (source, confidence,
 * when), so "where did this key come from" is answered in place.
 */
import type { components } from '$lib/api-types';
import { formatDuration } from '$lib/library/inspector-summary';

export type TrackInfoProvenance = components['schemas']['ProvenanceOut'];

/** Exactly what GET /api/v1/tracks/{id} sends (Sol P1 4285228857): derived from
 * the generated TrackOut contract, so a field the API adds is a type error in
 * TRACK_INFO_FIELD_ROWS below until it gets a row or an explicit null. */
export type TrackInfoRecord = components['schemas']['TrackOut'];

export interface TrackInfoPlays {
	opendj_play_count: number;
	rekordbox_play_count: number;
	opendj_last_played_at: string | null;
}

export interface TrackInfoRow {
	label: string;
	value: string;
	/** Hover text naming where the value came from, or null when none is stored. */
	provenance: string | null;
}

export const NOT_SET = 'not set';
/** Shown, with the raw value, when a stored timestamp does not parse. */
export const UNREADABLE_TIMESTAMP = 'unreadable timestamp';

function _yesNo(v: boolean | null | undefined): string {
	return v === null || v === undefined ? NOT_SET : v ? 'yes' : 'no';
}

function _tempo(v: number | null | undefined): string | null {
	return v === null || v === undefined ? null : v.toFixed(2);
}

function _text(v: string | null | undefined): string {
	return v === null || v === undefined || v.trim() === '' ? NOT_SET : v;
}

/** "rekordbox, confidence 0.92, Thu 8 Oct 2026 14:02 UTC" style hover. */
export function provenanceText(p: TrackInfoProvenance | undefined): string | null {
	if (p === undefined) return null;
	const parts = [`source ${p.source}`];
	if (p.confidence !== null && p.confidence !== undefined) parts.push(`confidence ${p.confidence}`);
	if (p.status !== 'ok') parts.push(`status ${p.status}${p.reason ? ` (${p.reason})` : ''}`);
	parts.push(`set ${p.modified_at}`);
	return parts.join(', ');
}

type RowBuilder = (
	track: TrackInfoRecord,
	plays: TrackInfoPlays | null,
	prov: (field: string) => string | null
) => TrackInfoRow;

/**
 * One row per TrackInfoRecord field, in display order (Sol P1 4284394507).
 * Typed as a Record over every key, so a field added to TrackInfoRecord
 * without a row here is a type error rather than a value the overlay
 * silently never shows (`-?` keeps an optional field required here too).
 * `provenance` is not a row of its own: it is the
 * hover on every other row, and its entry says so by returning null.
 */
/** The later of two play timestamps (Open DJ's and the record's, which can
 * come from rekordbox), so "Last played" is never a stale play when the other
 * source holds a newer one (Sol P2 4287872624, Codex P2 4287153656). A stamp
 * that does not parse is a broken contract, not a play: the row says so and
 * names it, rather than showing it, or the other stamp, as the last play
 * (Sol P1 4301475296). */
function _laterTimestamp(a: string | null, b: string | null): string | null {
	const bad = [a, b].find((v) => v !== null && Number.isNaN(Date.parse(v)));
	if (bad !== undefined) return `${UNREADABLE_TIMESTAMP} (${bad})`;
	if (a === null) return b;
	if (b === null) return a;
	return Date.parse(b) > Date.parse(a) ? b : a;
}

export const TRACK_INFO_FIELD_ROWS: { [K in keyof TrackInfoRecord]-?: RowBuilder | null } = {
	title: (t, _p, prov) => ({ label: 'Title', value: _text(t.title), provenance: prov('title') }),
	artist: (t, _p, prov) => ({ label: 'Artist', value: _text(t.artist), provenance: prov('artist') }),
	album: (t, _p, prov) => ({ label: 'Album', value: _text(t.album), provenance: prov('album') }),
	bpm: (t, _p, prov) => ({
		label: 'BPM',
		value: t.bpm === null || t.bpm === undefined ? NOT_SET : t.bpm.toFixed(2),
		provenance: prov('bpm')
	}),
	key: (t, _p, prov) => ({ label: 'Key', value: _text(t.key), provenance: prov('key') }),
	key_source: (t) => ({ label: 'Key source', value: _text(t.key_source), provenance: null }),
	// KEY-GUESS-01: a declined key keeps its best candidate, shown with "?" like the key cell.
	key_guess: (t) => ({
		label: 'Key guess',
		value: t.key_guess === null || t.key_guess === undefined ? NOT_SET : `${t.key_guess}?`,
		provenance: null
	}),
	key_guess_confidence: (t) => ({
		label: 'Key guess confidence',
		value:
			t.key_guess_confidence === null || t.key_guess_confidence === undefined
				? NOT_SET
				: `${Math.round(t.key_guess_confidence * 100)}%`,
		provenance: null
	}),
	duration_ms: (t, _p, prov) => ({
		label: 'Duration',
		value: t.duration_ms === null || t.duration_ms === undefined ? NOT_SET : formatDuration(t.duration_ms),
		provenance: prov('duration_ms')
	}),
	rating: (t, _p, prov) => ({
		label: 'Rating',
		value: t.rating === null || t.rating === undefined ? NOT_SET : `${t.rating} of 5`,
		provenance: prov('rating')
	}),
	tags: (t, _p, prov) => ({
		label: 'Tags',
		value: (t.tags ?? []).length === 0 ? NOT_SET : (t.tags ?? []).join(', '),
		provenance: prov('tags')
	}),
	notes: (t, _p, prov) => ({ label: 'Notes', value: _text(t.notes), provenance: prov('notes') }),
	file_path: (t, _p, prov) => ({ label: 'File', value: _text(t.file_path), provenance: prov('file_path') }),
	play_count: (t, plays) => ({
		label: 'Plays',
		value:
			plays === null
				? // The plays read failed, or the track is a USB-stick track with none:
					// the count is unknown. TrackOut's play_count is no fallback, since
					// GET /api/v1/tracks/{id} leaves it at the model default of 0, so it
					// would show a made-up 0 (Sol P1 4290983938, Codex P2 4291070831).
					NOT_SET
				: `${plays.opendj_play_count} in Open DJ, ${plays.rekordbox_play_count} in rekordbox`,
		provenance: null
	}),
	last_played_at: (t, plays) => ({
		label: 'Last played',
		value: _text(_laterTimestamp(plays?.opendj_last_played_at ?? null, t.last_played_at ?? null)),
		provenance: null
	}),
	created_at: (t) => ({ label: 'Added', value: t.created_at, provenance: null }),
	updated_at: (t) => ({ label: 'Updated', value: t.updated_at, provenance: null }),
	stable_id: (t) => ({ label: 'Track id', value: t.stable_id, provenance: null }),
	tempo_pref: (t, _p, prov) => {
		const p = t.tempo_pref;
		const parts =
			p === null || p === undefined
				? []
				: [
						['preferred', _tempo(p.regular)],
						['min', _tempo(p.min)],
						['max', _tempo(p.max)]
					].filter((e): e is [string, string] => e[1] !== null);
		return {
			label: 'Preferred tempo',
			value: parts.length === 0 ? NOT_SET : parts.map(([k, v]) => `${k} ${v}`).join(', '),
			provenance: prov('tempo_pref')
		};
	},
	has_rb_mapping: (t) => ({ label: 'In rekordbox', value: _yesNo(t.has_rb_mapping), provenance: null }),
	artwork_available: (t) => ({ label: 'Artwork', value: _yesNo(t.artwork_available), provenance: null }),
	lyrics_available: (t) => ({ label: 'Lyrics', value: _yesNo(t.lyrics_available), provenance: null }),
	auto_cues_available: (t) => ({ label: 'Auto cues', value: _yesNo(t.auto_cues_available), provenance: null }),
	stems_available: (t) => ({ label: 'Stems', value: _yesNo(t.stems_available), provenance: null }),
	// AGENT-31: integrated loudness, or why it is unknown (the reason the agent's trim match names), never a made-up value.
	loudness: (t) => ({
		label: 'Loudness',
		value:
			t.loudness === null || t.loudness === undefined
				? NOT_SET
				: t.loudness.lufs === null
					? _text(t.loudness.reason ?? t.loudness.status)
					: `${t.loudness.lufs.toFixed(1)} LUFS`,
		provenance: null
	}),
	provenance: null
};

export function trackInfoRows(track: TrackInfoRecord, plays: TrackInfoPlays | null): TrackInfoRow[] {
	const prov = (field: string): string | null => provenanceText(track.provenance?.[field]);
	const rows: TrackInfoRow[] = [];
	for (const build of Object.values(TRACK_INFO_FIELD_ROWS)) {
		if (build !== null) rows.push(build(track, plays, prov));
	}
	return rows;
}
