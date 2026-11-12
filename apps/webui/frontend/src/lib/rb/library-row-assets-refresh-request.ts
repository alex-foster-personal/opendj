/**
 * Ask visible library rows to re-fetch row-assets (vocals, stems, cover) for
 * specific stable_ids without a full library reload (issue #11422).
 * Local pub/sub only; not the server WebSocket bus.
 */
type Listener = (ids: readonly string[]) => void;

const listeners = new Set<Listener>();

export function requestLibraryRowAssetsRefresh(ids: readonly string[]): void {
	for (const listener of [...listeners]) {
		try {
			listener(ids);
		} catch (err) {
			console.error('requestLibraryRowAssetsRefresh: listener threw', err);
		}
	}
}

export function subscribeLibraryRowAssetsRefreshRequest(listener: Listener): () => void {
	listeners.add(listener);
	return () => {
		listeners.delete(listener);
	};
}
