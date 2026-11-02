/**
 * Mounts the Set notes host (the right-Cmd tap hotkey plus the pad it
 * toggles, SETNOTE-6340/6340b) on document.body from one deferred boot task, the
 * way the diagnostics consent dialog is mounted, so none of it is on any
 * first paint: the library surface has no gzip headroom for a feature that
 * draws nothing until it is asked for. The cost is that the tap does nothing
 * until the boot window closes. A failed load, here or of the pad the host
 * imports on first open, is handed to `onError` (the layout raises an error
 * toast) rather than leaving a hotkey that silently does nothing.
 */
import { bootScheduler } from '$lib/rb/boot-scheduler';

/** Returns the teardown: unmounts the host, or cancels a mount still queued. */
export function deferSetNotesHost(onError: (error: unknown) => void): () => void {
	let stopped = false;
	let teardown: (() => void) | null = null;
	bootScheduler.defer('set-notes:mount', () => {
		void Promise.all([import('svelte'), import('./SetNotesHost.svelte')])
			.then(([{ mount, unmount }, { default: Host }]) => {
				if (stopped) return;
				const instance = mount(Host, { target: document.body, props: { onError } });
				teardown = () => void unmount(instance);
			})
			.catch(onError);
	});
	return () => {
		stopped = true;
		teardown?.();
	};
}
