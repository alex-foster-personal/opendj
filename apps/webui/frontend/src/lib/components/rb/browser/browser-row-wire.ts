/**
 * Wire -> BrowserRow mapping for the library table (contract points 1 + 4).
 *
 * Moved out of BrowserPanel.svelte so the availability contract can be
 * tested on its own. PERF-RB-01 (issue #1037) made the server's disk truth a
 * budgeted, typed lane: `file_availability` is one of present / absent /
 * streaming / awaiting_volume / AVAILABILITY_PENDING, and `file_exists` is a
 * bool for every settled status and null ONLY while availability is pending.
 * The mapper enforces exactly that pairing: a pending row with null loads
 * (a big playlist has hundreds of them on a cold index), while null on a
 * settled row, or a missing status, still fails loudly as a broken backend
 * contract rather than being guessed into "missing" or "present".
 */
import {
	decodePreviewStrip,
	parseStemSummary,
	parseVocals,
	type FileAvailabilityStatus,
	type PlaylistTrackRowWire,
	type TrackListItemWire
} from '$lib/rb/api-rb';
import type { BrowserRow } from './pane-contract.svelte';

const FILE_AVAILABILITY_STATUSES: ReadonlySet<string> = new Set<FileAvailabilityStatus>([
	'present',
	'absent',
	'AVAILABILITY_PENDING',
	'streaming',
	'awaiting_volume'
]);

/** The row's availability pair, or null when the wire breaks the contract. */
export function wireAvailability(wire: {
	file_exists: unknown;
	file_availability: unknown;
}): Pick<BrowserRow, 'file_exists' | 'file_availability'> | null {
	const status = wire.file_availability;
	if (typeof status !== 'string' || !FILE_AVAILABILITY_STATUSES.has(status)) return null;
	const availability = status as FileAvailabilityStatus;
	if (availability === 'AVAILABILITY_PENDING') {
		return wire.file_exists === null
			? { file_exists: null, file_availability: availability }
			: null;
	}
	return typeof wire.file_exists === 'boolean'
		? { file_exists: wire.file_exists, file_availability: availability }
		: null;
}

/** A pending row's availability settled from its /rb-meta answer, or null
 * when there is nothing to settle (row already settled, or no rb-meta yet).
 *
 * The listing's row-hydration budget leaves most of a cold collection
 * AVAILABILITY_PENDING and nothing re-asks; /rb-meta's file_exists is a
 * full stat (its type cannot say pending), so it is disk truth, not a guess.
 * A settled row keeps its typed status (awaiting_volume, streaming). */
export function settledAvailabilityFromRbMeta(
	row: Pick<BrowserRow, 'file_exists' | 'file_availability'>,
	meta: { file_exists: boolean; is_streaming: boolean } | null
): Pick<BrowserRow, 'file_exists' | 'file_availability'> | null {
	if (meta === null || row.file_availability !== 'AVAILABILITY_PENDING') return null;
	if (meta.is_streaming) return { file_exists: false, file_availability: 'streaming' };
	else if (meta.file_exists) return { file_exists: true, file_availability: 'present' };
	else return { file_exists: false, file_availability: 'absent' };
}

export function rowFromPlaylistWire(wire: PlaylistTrackRowWire, order: number): BrowserRow {
	const availability = wireAvailability(wire);
	if (
		typeof wire.stable_id !== 'string' ||
		availability === null ||
		typeof wire.has_rb_mapping !== 'boolean'
	) {
		throw new Error(
			`hydrated playlist row ${order} malformed - backend contract point 4 not met`
		);
	}
	return {
		stable_id: wire.stable_id,
		item_id: wire.item_id ?? null,
		order,
		title: wire.title,
		artist: wire.artist,
		key: wire.key,
		bpm: wire.bpm,
		rating: wire.rating,
		etag: wire.etag,
		comments: wire.comments,
		duration_ms: wire.duration_ms,
		genre: wire.genre,
		genre_reason: wire.genre_reason ?? null,
		energy: wire.energy,
		energy_source: wire.energy_source,
		energy_reason: wire.energy_reason,
		key_status: wire.key_status ?? 'ok',
		key_reason: wire.key_reason ?? null,
		bpm_status: wire.bpm_status ?? 'ok',
		bpm_reason: wire.bpm_reason ?? null,
		loudness_status: wire.loudness_status ?? 'ok',
		loudness_reason: wire.loudness_reason ?? null,
		...availability,
		is_streaming: wire.is_streaming,
		is_remote: wire.is_remote === true,
		has_remote_copy: wire.has_remote_copy === true,
		cloud_transfer: wire.cloud_transfer ?? null,
		spotify_pending:
			wire.spotify_pending === true || wire.stable_id.startsWith('spotify-pending:'),
		streaming_provider: wire.streaming_provider ?? null,
		quality: wire.quality ?? null,
		play_count: typeof wire.play_count === 'number' ? wire.play_count : 0,
		strip: decodePreviewStrip(wire.preview_b64, wire.preview_max),
		vocals: parseVocals(wire.vocals),
		stems: parseStemSummary(wire.stems),
		has_rb_mapping: wire.has_rb_mapping,
		artwork_available: wire.artwork_available,
		artwork_status: wire.artwork_status,
		rb_meta: null,
		revealed: false,
		match_context: null,
		lyrics: wire.lyrics ?? null,
		grid_quality: wire.grid_quality ?? null,
		is_remix: wire.is_remix ?? null,
		is_radio_edit: wire.is_radio_edit ?? null
	};
}

export function rowFromListWire(track: TrackListItemWire, order: number): BrowserRow {
	const availability = wireAvailability(track);
	if (typeof track.stable_id !== 'string' || availability === null) {
		throw new Error(`tracks endpoint returned a non-track payload at row ${order}`);
	}
	return {
		stable_id: track.stable_id,
		item_id: null,
		order,
		// TrackOut spells its nullable fields optional; a BrowserRow wants one
		// spelling of "unknown", so absent collapses onto null here.
		title: track.title ?? null,
		artist: track.artist ?? null,
		key: track.key ?? null,
		bpm: track.bpm ?? null,
		rating: track.rating ?? null,
		// List items carry no ETag; rating edits lazily fetch one.
		etag: '',
		comments: track.notes ?? null,
		duration_ms: track.duration_ms ?? null,
		genre: track.genre ?? null,
		genre_reason: track.genre_reason ?? null,
		energy: track.energy,
		energy_source: track.energy_source,
		energy_reason: track.energy_reason,
		...availability,
		is_streaming: track.is_streaming,
		streaming_provider: track.streaming_provider,
		is_remote: track.is_remote === true,
		has_remote_copy: track.has_remote_copy === true,
		cloud_transfer: track.cloud_transfer ?? null,
		spotify_pending: track.stable_id.startsWith('spotify-pending:'),
		quality: track.quality ?? null,
		play_count: typeof track.play_count === 'number' ? track.play_count : 0,
		strip: decodePreviewStrip(track.preview_b64, track.preview_max),
		vocals: parseVocals(track.vocals),
		stems: parseStemSummary(track.stems),
		has_rb_mapping: track.has_rb_mapping,
		artwork_available: track.artwork_available,
		artwork_status: track.artwork_status,
		rb_meta: null,
		revealed: false,
		match_context: null,
		lyrics: track.lyrics ?? null,
		grid_quality: track.grid_quality ?? null,
		is_remix: track.is_remix ?? null,
		is_radio_edit: track.is_radio_edit ?? null
	};
}
