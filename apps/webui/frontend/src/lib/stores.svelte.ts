/** Runtime-lite shared state for banner warnings + toast stack.
 * Uses Svelte 5 runes so components can `$derive` and re-render cheaply.
 */
import { getHealth, type HealthOut } from './api';
import { reportClientError, type ClientErrorContext } from './client-error-reporting';

export type Toast = { id: number; message: string; kind: 'info' | 'error' };

let _toastSeq = 0;
export const toasts = $state<Toast[]>([]);

/** Default auto-dismiss delay when a caller does not name its own. */
export const TOAST_DEFAULT_MS = 5000;

/**
 * `context` rides the error report this toast already sends. An error toast is
 * the client's only route to the server-side client-error log, so a caller that
 * measured WHY it is raising the toast (deck load stage timings, say) attaches
 * it here rather than firing a second reportClientError of its own - two
 * reports per failure would land as two JSONL rows with the diagnosis on
 * neither, because reportClientError dedupes on `source`.
 */
export function pushToast(
	message: string,
	kind: 'info' | 'error' = 'info',
	dismissMs: number = TOAST_DEFAULT_MS,
	cause?: unknown,
	context: ClientErrorContext = {}
): void {
	if (!Number.isFinite(dismissMs) || dismissMs <= 0) {
		throw new RangeError(`pushToast: dismissMs must be a positive finite number, got ${dismissMs}`);
	}
	// P11-F03: capture *this* toast's id in the closure. The previous
	// implementation closed over the module-level `_toastSeq` counter,
	// which meant overlapping toasts would cause each timer to dismiss
	// the most-recently-pushed toast instead of the one that was
	// actually due to expire.
	const id = ++_toastSeq;
	toasts.push({ id, message, kind });
	if (kind === 'error') {
		// The caller's context is spread last so it can name its own `source`,
		// which is what keeps a deck-load failure filterable in the JSONL
		// instead of anonymous under 'toast'.
		reportClientError(cause ?? new Error(message), { source: 'toast', toast_id: id, ...context });
	}
	setTimeout(() => {
		const i = toasts.findIndex((t) => t.id === id);
		if (i >= 0) toasts.splice(i, 1);
	}, dismissMs);
}

export const health = $state<{ data: HealthOut | null; bindWarning: string | null }>(
	{ data: null, bindWarning: null }
);

export async function refreshHealth(): Promise<void> {
	try {
		const { health: data, bindWarning } = await getHealth();
		health.data = data;
		health.bindWarning = bindWarning;
	} catch (exc) {
		health.data = null;
	}
}
