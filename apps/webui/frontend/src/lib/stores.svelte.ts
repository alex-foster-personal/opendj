/** Runtime-lite shared state for banner warnings + toast stack.
 * Uses Svelte 5 runes so components can `$derive` and re-render cheaply.
 */
import { getHealth, type HealthOut } from './api';
import { reportClientError } from './client-error-reporting';
import { recordPerfEvent } from './rb/perf-event-log';

export type Toast = { id: number; message: string; kind: 'info' | 'error' };

let _toastSeq = 0;
export const toasts = $state<Toast[]>([]);

/** Default auto-dismiss delay when a caller does not name its own. */
export const TOAST_DEFAULT_MS = 5000;

/**
 * Every toast outlives its own on-screen dismissal in the perf-event-log
 * ring (localStorage + console) - toasts vanish after TOAST_DEFAULT_MS with
 * no other trace, which is exactly what made the "why didn't AutoPlay fire"
 * investigation on Mon 17 Aug 2026 into log archaeology instead of a lookup.
 * One chokepoint here covers every current and future pushToast call site.
 */
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
	recordPerfEvent(`toast-${kind}`, message, null, kind === 'error' ? 'error' : 'info');
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
