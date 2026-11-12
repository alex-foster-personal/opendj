/**
 * SECONDSCREEN-10757: the desktop shell reports which extra windows are open;
 * the performance IPC mirror trusts only those reports, not optimistic echo.
 */

import {
	nativeShellKind,
	WINDOW_LABELS,
	type Scope,
	type WindowLabel
} from '$lib/shell/native-shell';

function parseShellWindowLabels(raw: unknown): WindowLabel[] {
	if (!Array.isArray(raw)) return [];
	const allowed = new Set<string>(WINDOW_LABELS);
	const labels: WindowLabel[] = [];
	for (const entry of raw) {
		if (typeof entry !== 'string') continue;
		if (!allowed.has(entry)) {
			console.warn(`[shell-windows] ignoring unknown window label ${JSON.stringify(entry)}`);
			continue;
		}
		if (!labels.includes(entry as WindowLabel)) labels.push(entry as WindowLabel);
	}
	return labels.sort();
}

/** Subscribe to Tauri `opendj-shell-windows` and seed from `query_shell_window_labels`. */
export function installTauriShellWindowReporter(
	applyLabels: (labels: WindowLabel[]) => void,
	scope: Scope = globalThis
): () => void {
	if (nativeShellKind(scope) !== 'tauri') return () => {};

	let unlisten: (() => void) | null = null;
	let cancelled = false;

	void (async () => {
		const { invoke } = await import('@tauri-apps/api/core');
		const { listen } = await import('@tauri-apps/api/event');
		try {
			const initial = await invoke<string[]>('query_shell_window_labels');
			if (!cancelled) applyLabels(parseShellWindowLabels(initial));
			const stop = await listen<{ labels: string[] }>('opendj-shell-windows', (event) => {
				applyLabels(parseShellWindowLabels(event.payload.labels));
			});
			if (cancelled) {
				void Promise.resolve(stop()).catch(() => {});
				return;
			}
			unlisten = stop;
		} catch (exc) {
			console.error('[shell-windows] Tauri reporter failed to install', exc);
		}
	})();

	return () => {
		cancelled = true;
		const stop = unlisten;
		unlisten = null;
		if (stop !== null) {
			void Promise.resolve(stop()).catch((exc: unknown) => {
				console.error('[shell-windows] releasing the label listener failed', exc);
			});
		}
	};
}
