/**
 * Agent parity for the Settings engine row, installed for the page's lifetime
 * the moment the root layout loads, on every route (Sol P1, PR #6829).
 *
 * rust-mode.svelte.ts holds the engine state but is only reached through the
 * deferred Settings prefetch or the performance page, so installing the bridge
 * from there left `window.__mdtAudioEngine` undefined for the first seconds of
 * boot. This module is tiny and eager; each call loads rust-mode on first use
 * and answers from it, so both methods return promises.
 */
export interface AudioEngineBridge {
	/** Whether a restart would change the engine this page runs. */
	restartNeeded(): Promise<boolean>;
	/** Reload so the saved engine starts; rejects when there is nothing to load. */
	restartToLoad(): Promise<void>;
}

const loadRustMode = () => import('./rust-mode.svelte');

export const audioEngineBridge: AudioEngineBridge = {
	restartNeeded: async () => (await loadRustMode()).engineRestartNeeded(),
	restartToLoad: async () => (await loadRustMode()).restartToLoadEngine()
};

if (typeof window !== 'undefined') {
	(window as Window & { __mdtAudioEngine?: AudioEngineBridge }).__mdtAudioEngine = audioEngineBridge;
}
