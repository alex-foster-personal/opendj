/**
 * Local-only browser noise filters (OBS-07, OBS-5454).
 *
 * OBS-07: transient fetch/abort TypeErrors. Safari/WebKit reports
 * `Load failed` (Sentry OPEN-DJ-FE-F); Chromium reports `Failed to fetch`.
 * These are connectivity, not application bugs. Exact match only:
 * `Failed to fetch dynamically imported module` is a missing SPA chunk
 * and must still reach Sentry.
 *
 * OBS-5454: STATE-14 HTTP 503 forwarded as `RbApiError` (Sentry OPEN-DJ-FE-P,
 * sibling OPEN-DJ-BE-7B). Token-match `STATE_STORE_BUSY` only; a bare sqlite
 * `database is locked` still ships.
 *
 * Keep both predicates in lockstep with `apps/shared/telemetry/budget.py`.
 */

export const TRANSIENT_NETWORK_MESSAGES: ReadonlySet<string> = new Set([
	'Load failed',
	'Failed to fetch',
	'NetworkError when attempting to fetch resource.',
	'The user aborted a request.',
	'The operation was aborted.'
]);

const TYPE_PREFIXES = ['TypeError: ', 'NetworkError: ', 'AbortError: ', 'DOMException: '] as const;

/** Newer WebKit appends the unreachable host: `Load failed (127.0.0.1:7700)`
 * (Sentry OPEN-DJ-BE-4G). Anchored; keep in lockstep with budget.py. */
const WEBKIT_LOAD_FAILED_HOST = /^Load failed \([^()\s]+\)$/;

function isTransientText(text: string): boolean {
	return TRANSIENT_NETWORK_MESSAGES.has(text) || WEBKIT_LOAD_FAILED_HOST.test(text);
}

export function isTransientNetworkError(message: string, _name?: string | null): boolean {
	const text = message.trim();
	if (text.length === 0) return false;
	if (isTransientText(text)) return true;
	for (const prefix of TYPE_PREFIXES) {
		if (text.startsWith(prefix) && isTransientText(text.slice(prefix.length))) {
			return true;
		}
	}
	return false;
}

export const STATE_STORE_BUSY_TOKEN = 'STATE_STORE_BUSY';
const TOKEN_RE = /[A-Za-z0-9_]+/g;

export function isExpectedStateStoreBusy(message: string, name?: string | null): boolean {
	for (const text of [name, message]) {
		if (text == null || text === '') continue;
		const tokens = text.match(TOKEN_RE);
		if (tokens !== null && tokens.includes(STATE_STORE_BUSY_TOKEN)) return true;
	}
	return false;
}

export function sentryEventIsExpectedStateStoreBusy(event: {
	message?: unknown;
	exception?: { values?: Array<{ type?: unknown; value?: unknown }> };
}): boolean {
	for (const value of event.exception?.values ?? []) {
		if (isExpectedStateStoreBusy(String(value.value ?? ''), String(value.type ?? ''))) {
			return true;
		}
	}
	if (event.message != null && event.message !== '') {
		return isExpectedStateStoreBusy(String(event.message));
	}
	return false;
}

export function sentryEventIsTransientNetwork(event: {
	message?: unknown;
	exception?: { values?: Array<{ type?: unknown; value?: unknown }> };
}): boolean {
	for (const value of event.exception?.values ?? []) {
		if (isTransientNetworkError(String(value.value ?? ''), String(value.type ?? ''))) {
			return true;
		}
	}
	if (event.message != null && event.message !== '') {
		return isTransientNetworkError(String(event.message));
	}
	return false;
}
