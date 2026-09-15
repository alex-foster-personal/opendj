/** Plain module state for the INSTALL-21 quit confirmation overlay. */

let open = false;
let focusReturn: HTMLElement | null = null;
const listeners = new Set<() => void>();

function _notify(): void {
	for (const listener of listeners) listener();
}

export function subscribeQuitConfirmOpen(listener: () => void): () => void {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

export function isQuitConfirmOpen(): boolean {
	return open;
}

export function openQuitConfirm(focusTarget: HTMLElement | null): void {
	focusReturn = focusTarget;
	open = true;
	_notify();
}

export function closeQuitConfirm(): void {
	open = false;
	const target = focusReturn;
	focusReturn = null;
	target?.focus();
	_notify();
}

export const quitConfirmOverlay = {
	get open() {
		return open;
	}
};
