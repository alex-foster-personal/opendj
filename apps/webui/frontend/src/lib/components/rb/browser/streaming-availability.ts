import type { FileAvailabilityStatus } from '$lib/rb/file-availability';

/**
 * Streaming-service rows (rekordbox URIs such as tidal:tracks:99560085).
 *
 * The database and the API keep the generic state `streaming`. The browser
 * names the service from the URI scheme at read time (`tidal-streaming`)
 * and treats that row as a broken link Open DJ cannot play. A `present`
 * local file is never relabelled.
 */

const NAMED_WHEN_UNCLASSIFIED = new Set(['tidal', 'spotify', 'soundcloud', 'beatport']);

const DISPLAY_NAME: Record<string, string> = {
	tidal: 'Tidal',
	spotify: 'Spotify',
	soundcloud: 'SoundCloud',
	beatport: 'Beatport',
	http: 'HTTP',
	https: 'HTTPS'
};

export interface StreamingRowFacts {
	file_exists?: boolean | null | undefined;
	file_availability?: string | null | undefined;
	is_streaming?: boolean | null | undefined;
	file_path?: string | null | undefined;
	streaming_provider?: string | null | undefined;
	rb_meta?: { is_streaming?: boolean | null | undefined; folder_path?: string | null | undefined } | null | undefined;
}

/** Scheme of a streaming URI. A drive letter and `file:` are not schemes. */
export function uriScheme(path: string | null | undefined): string | null {
	if (path === null || path === undefined) return null;
	const match = /^([a-z][a-z0-9+.-]*):/i.exec(path.trim());
	if (match === null) return null;
	const scheme = match[1].toLowerCase();
	if (scheme.length < 2 || scheme === 'file') return null;
	return scheme;
}

export function isNamedStreamingStatus(status: string | null | undefined): boolean {
	return typeof status === 'string' && /^[a-z][a-z0-9+.-]*-streaming$/.test(status);
}

function schemeOf(row: StreamingRowFacts): string | null {
	const fromStatus = isNamedStreamingStatus(row.file_availability)
		? row.file_availability!.slice(0, -'-streaming'.length)
		: null;
	const provider =
		row.streaming_provider && row.streaming_provider !== 'unknown'
			? row.streaming_provider.toLowerCase()
			: null;
	return (
		uriScheme(row.file_path) ??
		uriScheme(row.rb_meta?.folder_path) ??
		provider ??
		fromStatus
	);
}

/** Wire `streaming` (or a missed service URI) becomes `{scheme}-streaming`. */
export function nameStreamingAvailability(
	status: string,
	path?: string | null,
	provider?: string | null
): FileAvailabilityStatus {
	if (status === 'present' || status === 'AVAILABILITY_PENDING' || status === 'awaiting_volume') {
		return status;
	}
	const named = isNamedStreamingStatus(status);
	if (status !== 'streaming' && status !== 'absent' && !named) {
		return status as FileAvailabilityStatus;
	}
	const scheme =
		uriScheme(path) ??
		(provider && provider !== 'unknown' ? provider.toLowerCase() : null) ??
		(named ? status.slice(0, -'-streaming'.length) : null);
	if (scheme === null) return status === 'absent' ? status : 'streaming';
	if (status === 'absent' && !NAMED_WHEN_UNCLASSIFIED.has(scheme)) return status;
	return `${scheme}-streaming`;
}

export function serviceDisplayName(scheme: string): string {
	return DISPLAY_NAME[scheme] ?? scheme.charAt(0).toUpperCase() + scheme.slice(1);
}

/** True when this row is a streaming service, including a legacy `streaming` value. */
export function isStreamingAvailability(row: StreamingRowFacts): boolean {
	if (row.file_availability === 'streaming' || isNamedStreamingStatus(row.file_availability)) {
		return true;
	}
	if (row.is_streaming === true || row.rb_meta?.is_streaming === true) return true;
	if (row.file_availability === 'present' && row.file_exists === true) return false;
	const scheme = uriScheme(row.file_path) ?? uriScheme(row.rb_meta?.folder_path);
	return scheme !== null && NAMED_WHEN_UNCLASSIFIED.has(scheme);
}

/** "Tidal streaming track: Open DJ can't play streaming services". */
export function streamingLoadRefusal(row: StreamingRowFacts = {}): string {
	const scheme = schemeOf(row);
	if (scheme === null) return "Streaming track: Open DJ can't play streaming services";
	return `${serviceDisplayName(scheme)} streaming track: Open DJ can't play streaming services`;
}
