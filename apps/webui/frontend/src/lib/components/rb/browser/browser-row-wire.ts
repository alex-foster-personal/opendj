/**
 * Wire -> BrowserRow mapping for the library table (contract points 1 + 4).
 *
 * Moved out of BrowserPanel.svelte so the availability contract can be
 * tested on its own. PERF-RB-01 (issue #1037) made the server's disk truth a
 * budgeted, typed lane: `file_availability` is one of present / absent /
 * streaming / awaiting_volume / AVAILABILITY_PENDING, and `file_exists` is a
 * bool for every settled status and null ONLY while availability is pending.
 * The mapper enforces exactly that pairing: a pending row carries null
 * file_exists. Loading that row is refused (the load is not the probe);
 * null on a settled row, or a missing status, still fails loudly as a
 * broken backend contract rather than being guessed into "missing" or
 * "present".
 */

import {
	decodePreviewStrip,
	parseStemSummary,
	parseVocals,
	type FileAvailabilityStatus,
	type PlaylistTrackRowWire,
	type TrackIndexItemWire,
	type TrackListItemWire
} from '$lib/rb/api-rb';
import type { BrowserRow } from './pane-contract.svelte';
import {
	isNamedStreamingStatus,
	isStreamingAvailability,
	nameStreamingAvailability,
	streamingLoadRefusal,
	type StreamingRowFacts
} from './streaming-availability';

/**
 * The greyed library row and the Broken checkbox share this predicate.
 * True for no audio Open DJ can play: an absent file, an unchecked row
 * (`AVAILABILITY_PENDING`), or a streaming-service URI. Spotify placeholders,
 * a pulled stick, and awaiting-volume rows stay out.
 */
export function rowRendersUnavailable(row: {
	file_exists: boolean | null;
	file_availability?: string | null;
	is_streaming?: boolean | null;
	is_remote?: boolean | null;
	spotify_pending?: boolean | null;
	stable_id?: string;
	file_path?: string | null;
	rb_meta?: { is_streaming?: boolean | null; folder_path?: string | null } | null;
}): boolean {
	if (row.spotify_pending === true) return false;
	if (typeof row.stable_id === 'string' && row.stable_id.startsWith('spotify-pending:')) return false;
	if (isStreamingAvailability(row)) return true;
	if (row.file_availability === 'awaiting_volume' || row.is_remote === true) return false;
	return row.file_exists === false || row.file_availability === 'AVAILABILITY_PENDING';
}

/** Why this row must not reach a deck, or null when its audio is present. */
export function libraryAudioLoadRefusal(row: {
	file_exists: boolean | null;
	file_availability?: string | null;
	is_streaming?: boolean | null;
	file_path?: string | null;
	streaming_provider?: string | null;
	rb_meta?: { is_streaming?: boolean | null; folder_path?: string | null } | null;
}): string | null {
	if (isStreamingAvailability(row)) return streamingLoadRefusal(row);
	// A row that only says the file is on disk (no typed status yet) is loadable.
	// Pending and awaiting-volume stay refused until the status is present.
	if (
		row.file_exists === true &&
		(row.file_availability == null || row.file_availability === 'present')
	) {
		return null;
	}
	if (row.file_exists === false || row.file_availability === 'absent') {
		return 'cannot load: audio file missing on disk (broken link)';
	}
	return 'cannot load: audio on this machine has not been confirmed';
}

/** Row tooltip: the streaming sentence, the missing-file sentence, or pending. */
export function libraryRowHoverTitle(row: StreamingRowFacts & {
	file_exists: boolean | null;
}): string | undefined {
	if (isStreamingAvailability(row)) return streamingLoadRefusal(row);
	if (row.file_exists === false) return 'cannot load: audio file missing on disk (broken link)';
	if (row.file_availability === 'AVAILABILITY_PENDING') {
		return 'cannot load: audio on this machine has not been confirmed';
	}
	return undefined;
}

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
	if (
		typeof status !== 'string' ||
		(!FILE_AVAILABILITY_STATUSES.has(status) && !isNamedStreamingStatus(status))
	) {
		return null;
	}
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

/** Write rb-meta disk truth onto a pending row and name a streaming URI.
 * No-op once the row has already settled. */
export function applySettledAvailability(row: {
	file_exists: boolean | null;
	file_availability?: BrowserRow['file_availability'] | null;
	file_path?: string | null;
	rb_meta?: { file_exists: boolean; is_streaming: boolean; folder_path?: string | null } | null;
}): void {
	const settled = settledAvailabilityFromRbMeta(
		{
			file_exists: row.file_exists,
			file_availability: row.file_availability ?? 'AVAILABILITY_PENDING'
		},
		row.rb_meta ?? null
	);
	if (settled === null) return;
	const path = row.file_path ?? row.rb_meta?.folder_path ?? null;
	Object.assign(row, {
		...settled,
		file_availability: nameStreamingAvailability(settled.file_availability, path, null)
	});
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
		genre_guess: wire.genre_guess ?? null,
		energy: wire.energy,
		energy_source: wire.energy_source,
		energy_reason: wire.energy_reason,
		key_status: wire.key_status ?? 'ok',
		key_reason: wire.key_reason ?? null,
		bpm_status: wire.bpm_status ?? 'ok',
		bpm_reason: wire.bpm_reason ?? null,
		bpm_source: wire.bpm_source ?? null,
		bpm_method: wire.bpm_method ?? null,
		bpm_confidence: wire.bpm_confidence ?? null,
		bpm_confidence_error: wire.bpm_confidence_error ?? null,
		loudness_status: wire.loudness_status ?? 'ok',
		loudness_reason: wire.loudness_reason ?? null,
		...availability,
		file_availability: nameStreamingAvailability(
			availability.file_availability,
			typeof wire.file_path === 'string' ? wire.file_path : null,
			wire.streaming_provider
		),
		file_path: typeof wire.file_path === 'string' ? wire.file_path : null,
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

/**
 * The listing's own streaming verdict (issue #3934). `TrackListItemOut`
 * once carried no `is_streaming` field (CHROME-02 added it on the Preview
 * branch; this verdict still wins when it says streaming), so All Tracks rows used to start at null
 * and learn they were streaming only once rb-meta hydrated, i.e. after the
 * operator had already tried to load or drag them; until then a streaming row
 * read as an ordinary broken link. The server already classifies a streaming
 * URI as `file_availability: 'streaming'`, so that status settles the flag up
 * front. Any other status stays null ("not known to be streaming"), which
 * keeps the lazy rb-meta fallback for the rest unchanged.
 */
export { nameStreamingAvailability } from './streaming-availability';

export function listRowIsStreaming(
	availability: FileAvailabilityStatus | null | undefined
): true | null {
	if (availability === 'streaming' || isNamedStreamingStatus(availability)) return true;
	return null;
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
		genre_guess: track.genre_guess ?? null,
		energy: track.energy,
		energy_source: track.energy_source,
		energy_reason: track.energy_reason,
		bpm_source: track.bpm_source ?? null,
		bpm_method: track.bpm_method ?? null,
		bpm_confidence: track.bpm_confidence ?? null,
		bpm_confidence_error: track.bpm_confidence_error ?? null,
		...availability,
		file_path: typeof track.file_path === 'string' ? track.file_path : null,
		file_availability: nameStreamingAvailability(
			availability.file_availability,
			typeof track.file_path === 'string' ? track.file_path : null,
			track.streaming_provider
		),
		// CHROME-02 wire flag, with the listing's own availability verdict
		// (issue #3934) winning when it already says streaming.
		is_streaming:
			listRowIsStreaming(
				nameStreamingAvailability(
					availability.file_availability,
					typeof track.file_path === 'string' ? track.file_path : null,
					track.streaming_provider
				)
			) ?? track.is_streaming ?? null,
		streaming_provider: track.streaming_provider ?? null,
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

/** What a library index row says before `POST /library/row-assets` answers for
 * it (LIBM-172): no strip yet, no vocal regions yet, cover not yet checked. No
 * sort, filter or search reads these fields (library-index-lazy-assets test). */
export const INDEX_ROW_DEFERRED = {
	preview_b64: null,
	preview_max: null,
	vocals: { status: 'not_analyzed' },
	artwork_available: null,
	artwork_status: 'unresolved'
} as const satisfies Pick<TrackListItemWire, 'preview_b64' | 'preview_max' | 'vocals' | 'artwork_available' | 'artwork_status'>;

/** A library index row, or a full listing row (the boot first page), as a BrowserRow. */
export function rowFromIndexWire(item: TrackIndexItemWire | TrackListItemWire, order: number): BrowserRow {
	if ('artwork_status' in item) return rowFromListWire(item, order);
	return rowFromListWire({ ...item, ...INDEX_ROW_DEFERRED, provenance: {} }, order);
}
