/**
 * Keys pressed on the compatible dropdown before its lazy panel has mounted
 * (PR #6574, review 4393984663). The panel chunk loads on first open, so a T,
 * M or navigation key pressed in that gap is held here and replayed, in order,
 * once the panel binds. Keys the panel would not handle are never held.
 */
export interface BufferableKey {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	preventDefault(): void;
}

/** The keys CompatibleFilterPanel.handleKey acts on (T / M and arrows + WASD). */
export const COMPAT_PANEL_KEYS: ReadonlySet<string> = new Set([
	't', 'm', 'arrowleft', 'arrowright', 'arrowup', 'arrowdown', 'a', 'd', 'w', 's'
]);

export class PendingKeyBuffer<E extends BufferableKey> {
	#keys: E[] = [];

	/** Hold a key the panel will want; true when held (and its default stopped). */
	hold(e: E): boolean {
		if (e.metaKey || e.ctrlKey || e.altKey) return false;
		if (!COMPAT_PANEL_KEYS.has(e.key.toLowerCase())) return false;
		e.preventDefault();
		this.#keys.push(e);
		return true;
	}

	/** Replay every held key, oldest first, and empty the buffer. */
	flush(handle: (e: E) => void): void {
		const keys = this.#keys;
		this.#keys = [];
		for (const e of keys) handle(e);
	}

	/** Drop held keys (the dropdown closed before the panel arrived). */
	clear(): void {
		this.#keys = [];
	}

	get size(): number {
		return this.#keys.length;
	}
}
