/** Shared command path from top-bar text intents to the active browser pane. */

export interface BrowserSearchRequest {
	query: string;
	revision: number;
}

type BrowserSearchListener = (request: BrowserSearchRequest) => void;

let currentRequest: BrowserSearchRequest = { query: '', revision: 0 };
const listeners = new Set<BrowserSearchListener>();

export function requestBrowserSearch(query: string): BrowserSearchRequest {
	const normalized = query.trim();
	if (normalized === '') throw new Error('browser search query must be non-empty');
	currentRequest = { query: normalized, revision: currentRequest.revision + 1 };
	for (const listener of listeners) listener(currentRequest);
	return currentRequest;
}

export function subscribeBrowserSearch(listener: BrowserSearchListener): () => void {
	listeners.add(listener);
	listener(currentRequest);
	return () => listeners.delete(listener);
}
