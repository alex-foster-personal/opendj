/** Shared command path from top-bar text intents to the active browser pane. */

export interface BrowserSearchRequest {
	query: string;
	revision: number;
	result: BrowserSearchResult | null;
}

export interface BrowserSearchResult {
	fallback: boolean;
	ignoredFilters: string[];
	rowCount: number;
}

type BrowserSearchListener = (request: BrowserSearchRequest) => void;
type BrowserSearchResultListener = (request: BrowserSearchRequest) => void;

let currentRequest: BrowserSearchRequest = { query: '', revision: 0, result: null };
const listeners = new Set<BrowserSearchListener>();
const resultListeners = new Set<BrowserSearchResultListener>();

export function requestBrowserSearch(query: string): BrowserSearchRequest {
	const normalized = query.trim();
	if (normalized === '') throw new Error('browser search query must be non-empty');
	if (listeners.size === 0) throw new Error('browser search has no mounted result reporter');
	currentRequest = {
		query: normalized,
		revision: currentRequest.revision + 1,
		result: null
	};
	for (const listener of listeners) listener(currentRequest);
	return currentRequest;
}

/** Browser subscribers publish the rendered outcome for command callers. */
export function reportBrowserSearchResult(
	request: BrowserSearchRequest,
	result: BrowserSearchResult
): void {
	if (request.revision !== currentRequest.revision) {
		throw new Error('cannot report a stale browser search result');
	}
	request.result = result;
	for (const listener of resultListeners) listener(request);
}

/** True while `request` is still the search the browser owes an answer for. A
 * whole-collection FTS that a newer command superseded must check this: its
 * rows are not the current answer, and reporting them trips the stale guard
 * below inside an unawaited promise, which surfaces as an unhandled
 * rejection rather than as a handled failure. */
export function isCurrentBrowserSearch(request: BrowserSearchRequest): boolean {
	return request.revision === currentRequest.revision;
}

export function subscribeBrowserSearch(listener: BrowserSearchListener): () => void {
	listeners.add(listener);
	listener(currentRequest);
	return () => listeners.delete(listener);
}

/** Observe command results that may arrive after an FTS collection search. */
export function subscribeBrowserSearchResult(listener: BrowserSearchResultListener): () => void {
	resultListeners.add(listener);
	return () => resultListeners.delete(listener);
}
