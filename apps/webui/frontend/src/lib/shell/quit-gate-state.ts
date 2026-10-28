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

const quitVetoes = new Set<() => boolean>();

/** Something that must not be torn down mid-operation (a USB write) registers
 * a veto: it returns true, after showing why, to refuse a user's quit. */
export function registerQuitVeto(veto: () => boolean): () => void {
	quitVetoes.add(veto);
	return () => quitVetoes.delete(veto);
}

/** True when any registered veto refuses the quit right now. */
export function quitVetoed(): boolean {
	for (const veto of quitVetoes) {
		if (veto()) return true;
	}
	return false;
}

export const quitConfirmOverlay = {
	get open() {
		return open;
	}
};
