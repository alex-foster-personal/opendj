/** Runtime-lite shared state for banner warnings + toast stack.
 * Uses Svelte 5 runes so components can `$derive` and re-render cheaply.
 */
import { getHealth, type HealthOut } from './api';
import { reportClientError } from './client-error-reporting';

export type Toast = { id: number; message: string; kind: 'info' | 'error' };

let _toastSeq = 0;
export const toasts = $state<Toast[]>([]);

/** Default auto-dismiss delay when a caller does not name its own. */
export const TOAST_DEFAULT_MS = 5000;

export function pushToast(
	message: string,
	kind: 'info' | 'error' = 'info',
	dismissMs: number = TOAST_DEFAULT_MS,
	cause?: unknown
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
		reportClientError(cause ?? new Error(message), { source: 'toast', toast_id: id });
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
