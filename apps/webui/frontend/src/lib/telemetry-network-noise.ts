/**
 * Transient browser fetch/abort TypeErrors (OBS-07).
 *
 * Safari/WebKit reports `Load failed` (Sentry OPEN-DJ-FE-F); Chromium reports
 * `Failed to fetch`. These are connectivity, not application bugs. Keep the
 * message set in lockstep with `apps/shared/telemetry/budget.py`. Exact match
 * only: `Failed to fetch dynamically imported module` is a missing SPA chunk
 * and must still reach Sentry.
 */

export const TRANSIENT_NETWORK_MESSAGES: ReadonlySet<string> = new Set([
	'Load failed',
	'Failed to fetch',
	'NetworkError when attempting to fetch resource.',
	'The user aborted a request.',
	'The operation was aborted.'
]);

const TYPE_PREFIXES = ['TypeError: ', 'NetworkError: ', 'AbortError: ', 'DOMException: '] as const;

export function isTransientNetworkError(message: string, _name?: string | null): boolean {
	const text = message.trim();
	if (text.length === 0) return false;
	if (TRANSIENT_NETWORK_MESSAGES.has(text)) return true;
	for (const prefix of TYPE_PREFIXES) {
		if (text.startsWith(prefix) && TRANSIENT_NETWORK_MESSAGES.has(text.slice(prefix.length))) {
			return true;
		}
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
