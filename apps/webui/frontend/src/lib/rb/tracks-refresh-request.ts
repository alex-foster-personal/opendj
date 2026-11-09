/**
 * A component asks BrowserPanel to refetch library rows (issue #9653).
 * Local only; not the server WS bus.
 */
type Listener = (ids: readonly string[]) => void;

const listeners = new Set<Listener>();

export function requestTracksRefresh(ids: readonly string[]): void {
	for (const listener of [...listeners]) {
		try {
			listener(ids);
		} catch (err) {
			console.error('requestTracksRefresh: listener threw', err);
		}
	}
}

export function subscribeTracksRefreshRequest(listener: Listener): () => void {
	listeners.add(listener);
	return () => {
		listeners.delete(listener);
	};
}
