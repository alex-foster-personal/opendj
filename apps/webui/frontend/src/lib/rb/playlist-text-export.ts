/**
 * LIBORG-10314: download a playlist as a tab-separated text track list
 * (rekordbox .txt export). The file is built by
 * `GET /api/v1/playlists/{id}/export.txt`, the same route
 * `opendj playlist export <id> --format txt` reads, so the menu item and an
 * agent get byte-identical files.
 */
import { API_BASE } from '$lib/api/base';

export function playlistTextExportUrl(playlistId: string, base: string = API_BASE): string {
	return `${base}/api/v1/playlists/${encodeURIComponent(playlistId)}/export.txt`;
}

/** Start the download; the route's Content-Disposition names the file. */
export function downloadPlaylistText(playlistId: string, doc: Document = document): void {
	const link = doc.createElement('a');
	link.href = playlistTextExportUrl(playlistId);
	link.download = '';
	link.rel = 'noopener';
	doc.body.appendChild(link);
	link.click();
	link.remove();
}
