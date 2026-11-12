/** Pure helpers for set tracklist copy and download (issue #10391). */

import { SETS_API_BASE, type TracklistCopyFormat, type TracklistFileFormat } from './sets-api';

export const COPY_FORMATS: readonly { format: TracklistCopyFormat; label: string }[] = [
	{ format: 'txt', label: 'Text' },
	{ format: 'md', label: 'Markdown' },
	{ format: 'csv', label: 'CSV' },
	{ format: 'timestamped', label: 'Timestamped' },
	{ format: 'soundcloud', label: 'SoundCloud' }
];

export const DOWNLOAD_FORMATS: readonly { format: TracklistFileFormat; label: string }[] = [
	{ format: 'cue', label: '.cue' },
	{ format: 'm3u8', label: '.m3u8' }
];

export const HIDDEN_HINT = 'Hidden from tracklists';
export const EMPTY_HINT = 'No kept plays in this session, so there is no tracklist.';

export function tracklistExportUrl(sessionId: string, format: TracklistFileFormat): string {
	return `${SETS_API_BASE}/api/sets/${encodeURIComponent(sessionId)}/tracklist.${format}`;
}

export function tracklistFileName(sessionId: string, format: TracklistFileFormat): string {
	return `${sessionId}.${format}`;
}

export function rowArtistTitle(row: {
	hidden: boolean;
	artist: string | null;
	title: string | null;
	display_name: string;
}): { text: string; hidden: boolean } {
	if (row.hidden) {
		return { text: 'ID - ID', hidden: true };
	}
	return { text: row.display_name, hidden: false };
}

export async function copyTracklistAs(
	sessionId: string,
	format: TracklistCopyFormat,
	deps: {
		fetchText: (id: string, f: TracklistCopyFormat) => Promise<string>;
		writeClipboard: (text: string) => Promise<void>;
		notify: (message: string, kind?: 'info' | 'error') => void;
	}
): Promise<boolean> {
	const entry = COPY_FORMATS.find((item) => item.format === format);
	const label = entry?.label ?? format;
	try {
		const body = await deps.fetchText(sessionId, format);
		await deps.writeClipboard(body);
		deps.notify(`Tracklist copied as ${label}`);
		return true;
	} catch (error) {
		deps.notify(`Copy as ${label} failed: ${error}`, 'error');
		return false;
	}
}

export function downloadTracklist(
	sessionId: string,
	format: TracklistFileFormat,
	doc: Document = document
): HTMLAnchorElement {
	const anchor = doc.createElement('a');
	anchor.href = tracklistExportUrl(sessionId, format);
	anchor.download = tracklistFileName(sessionId, format);
	anchor.rel = 'noopener';
	doc.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
	return anchor;
}
