/**
 * Single polite live region for deck / hot-cue announcements (issue #10365).
 * No DOM access at import time.
 */

export const ANNOUNCE_DEDUPE_MS = 500;

let mountedNode: HTMLElement | null = null;
let lastText = '';
let lastAt = 0;

function nowMs(): number {
	if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
		return performance.now();
	}
	return Date.now();
}

function scheduleText(node: HTMLElement, text: string): void {
	const write = () => {
		node.textContent = text;
		const prev = Number.parseInt(node.dataset.announceCount ?? '0', 10);
		node.dataset.announceCount = String(Number.isFinite(prev) ? prev + 1 : 1);
	};
	node.textContent = '';
	setTimeout(write, 0);
}

/** Announce `text` on the mounted live region; dedupes identical text within 500 ms. */
export function announce(text: string): void {
	const trimmed = text.trim();
	if (trimmed === '') return;
	const t = nowMs();
	if (trimmed === lastText && t - lastAt < ANNOUNCE_DEDUPE_MS) return;
	lastText = trimmed;
	lastAt = t;
	if (mountedNode === null) return;
	scheduleText(mountedNode, trimmed);
}

/** Svelte action: mounts the one allowed announcer node. */
export function announcer(node: HTMLElement): { destroy(): void } {
	if (mountedNode !== null) {
		throw new Error('a11y announcer is already mounted');
	}
	mountedNode = node;
	return {
		destroy() {
			if (mountedNode === node) mountedNode = null;
		}
	};
}

/** Unit-test seam: clears module state. */
export function _resetAnnouncerForTests(): void {
	mountedNode = null;
	lastText = '';
	lastAt = 0;
}
