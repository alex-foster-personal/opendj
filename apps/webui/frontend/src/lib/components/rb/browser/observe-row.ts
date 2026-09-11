/**
 * One-shot row-visibility observer for TrackTable.
 *
 * IntersectionObserver is shared per factory instance. The Svelte action
 * returns update() so a reused keyed <tr> (same stable_id:order across a
 * pane switch) rebinds the WeakMap to the current row object and
 * re-observes. Without that, the one-shot unobserve from the previous pane
 * leaves the new BrowserRow blank.
 *
 * Do not call onRowVisible from update(): off-screen reused rows wait for
 * intersection. Same-reference update must not re-observe, or Svelte
 * re-evaluating the action expression would re-hydrate the same object.
 */

export function createRowVisibilityObserver<T>(opts: {
	onRowVisible: (row: T) => void;
	rootMargin?: string;
}): {
	observeRow(node: HTMLElement, row: T): { update(row: T): void; destroy(): void };
	disconnect(): void;
} {
	let onRowVisible = opts.onRowVisible;
	const rootMargin = opts.rootMargin ?? '120px 0px';
	const rowByEl = new WeakMap<Element, T>();
	let observer: IntersectionObserver | null = null;

	function _ensureObserver(): IntersectionObserver | null {
		if (typeof IntersectionObserver === 'undefined') return null; // SSR guard
		if (observer === null) {
			observer = new IntersectionObserver(
				(entries) => {
					for (const entry of entries) {
						if (!entry.isIntersecting) continue;
						const row = rowByEl.get(entry.target);
						observer?.unobserve(entry.target);
						if (row !== undefined) onRowVisible(row);
					}
				},
				{ rootMargin }
			);
		}
		return observer;
	}

	function observeRow(node: HTMLElement, row: T): { update(row: T): void; destroy(): void } {
		rowByEl.set(node, row);
		_ensureObserver()?.observe(node);
		return {
			update(next: T): void {
				rowByEl.set(node, next);
				if (next !== row) {
					row = next;
					const live = _ensureObserver();
					live?.unobserve(node);
					live?.observe(node);
				}
			},
			destroy(): void {
				observer?.unobserve(node);
			}
		};
	}

	function disconnect(): void {
		observer?.disconnect();
	}

	return { observeRow, disconnect };
}
