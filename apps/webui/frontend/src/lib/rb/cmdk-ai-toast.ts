/**
 * CMDK-21: the slot the Cmd K AI search's no-key toast goes through. The bar
 * sets it from the page's `pushToast`, so neither the lazy bar nor the "/"
 * first-paint closure imports stores.svelte for it; with nothing wired the
 * message is thrown, never dropped.
 */
let _toast: ((message: string) => void) | null = null;

export function setCmdkAiToast(toast: (message: string) => void): void {
	_toast = toast;
}

export function toastCmdkAi(message: string): void {
	if (_toast === null) throw new Error(`Cmd K AI search has no toast wired for: ${message}`);
	_toast(message);
}
