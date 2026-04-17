/** Runtime-lite shared state for banner warnings + toast stack.
 * Uses Svelte 5 runes so components can `$derive` and re-render cheaply.
 */
import { getHealth, type HealthOut } from './api';

export type Toast = { id: number; message: string; kind: 'info' | 'error' };

let _toastSeq = 0;
export const toasts = $state<Toast[]>([]);

export function pushToast(message: string, kind: 'info' | 'error' = 'info'): void {
	toasts.push({ id: ++_toastSeq, message, kind });
	// Auto-dismiss after 5s.
	setTimeout(() => {
		const i = toasts.findIndex((t) => t.id === _toastSeq);
		if (i >= 0) toasts.splice(i, 1);
	}, 5000);
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
