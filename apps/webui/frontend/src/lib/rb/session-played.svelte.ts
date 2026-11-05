/**
 * SESSION-PLAYED-01 rune wrapper: one tracker per page, booted on first use.
 * `sessionPlayed.visible` is what the library paints green; `phase` drives the
 * "Restore last session from <weekday time>?" banner.
 */
import { API_BASE } from '$lib/api/base';
import { pushToast } from '$lib/stores.svelte';
import {
	SessionPlayedTracker,
	type SessionPlayedDoc,
	type SessionPlayedPhase
} from './session-played';

async function send(method: 'GET' | 'POST', path: string, body?: unknown): Promise<SessionPlayedDoc> {
	const init: RequestInit =
		body === undefined
			? { method }
			: { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
	const response = await fetch(`${API_BASE}${path}`, init);
	if (!response.ok) throw new Error(`${method} ${path} returned ${response.status}: ${await response.text()}`);
	return (await response.json()) as SessionPlayedDoc;
}

export const sessionPlayed = $state<{
	phase: SessionPlayedPhase;
	visible: ReadonlySet<string>;
	doc: SessionPlayedDoc | null;
}>({ phase: 'loading', visible: new Set(), doc: null });

let lastError: string | null = null;
const tracker = new SessionPlayedTracker(
	{
		get: () => send('GET', '/api/v1/session/played'),
		add: (stableId) => send('POST', '/api/v1/session/played', { stable_id: stableId }),
		resume: () => send('POST', '/api/v1/session/resume'),
		startNew: () => send('POST', '/api/v1/session/new')
	},
	() => {
		sessionPlayed.phase = tracker.phase;
		sessionPlayed.visible = tracker.visibleIds;
		sessionPlayed.doc = tracker.doc;
		if (tracker.error !== null && tracker.error !== lastError) {
			pushToast(tracker.error, 'error');
		}
		lastError = tracker.error;
	}
);

let booted = false;
function ensureBooted(): void {
	if (booted || typeof window === 'undefined') return;
	booted = true;
	void tracker.boot();
}

/** Mark the ids now on decks as played this session (idempotent). */
export function recordSessionPlayed(ids: Iterable<string>): void {
	ensureBooted();
	for (const id of ids) tracker.record(id);
}

export function restoreLastSession(): Promise<void> {
	return tracker.resume();
}

export function startNewSession(): Promise<void> {
	return tracker.startNew();
}
