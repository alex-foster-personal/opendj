/**
 * WAI-ARIA Authoring Practices Guide Dialog (Modal) pattern: Tab and Shift+Tab
 * cycle inside the dialog; focus returns to the invoking element on destroy.
 * Issue #10364, requirement A11Y-11275 (predicted PR id).
 *
 * Tab order follows DOM order only; positive tabindex values are not re-sorted.
 */
export interface TabStopCandidate {
	/** :disabled (incl. inside a disabled fieldset). */
	disabled: boolean;
	/** The element's tabIndex property (-1 = focusable but not a Tab stop). */
	tabIndex: number;
	/** The element or an ancestor carries aria-hidden="true" or inert. */
	ariaHidden: boolean;
	/** Rendered (not display:none / visibility:hidden). */
	rendered: boolean;
}

export function isTabStop(c: TabStopCandidate): boolean {
	return !c.disabled && c.tabIndex >= 0 && !c.ariaHidden && c.rendered;
}

/**
 * Next Tab stop in DOM order. Wraps from last to first (Tab) and first to last
 * (Shift+Tab). Ignores positive tabindex ordering.
 */
export function nextTabStop<T extends TabStopCandidate>(
	list: readonly T[],
	current: T | null,
	backwards: boolean
): T | null {
	const stops = list.filter(isTabStop);
	if (stops.length === 0) return null;
	if (stops.length === 1) return stops[0];

	if (current === null) {
		return backwards ? stops[stops.length - 1]! : stops[0]!;
	}

	const currentIndex = list.indexOf(current);
	if (currentIndex === -1) {
		return backwards ? stops[stops.length - 1]! : stops[0]!;
	}

	const len = list.length;
	const step = backwards ? -1 : 1;
	for (let i = 1; i <= len; i++) {
		const idx = (currentIndex + i * step + len) % len;
		const candidate = list[idx]!;
		if (isTabStop(candidate)) return candidate;
	}
	return null;
}

export const FOCUSABLE_SELECTOR =
	'a[href], area[href], button, input:not([type="hidden"]), select, textarea, iframe, summary, [contenteditable]:not([contenteditable="false"]), [tabindex]';

export function collectTabStops(root: HTMLElement): Array<TabStopCandidate & { el: HTMLElement }> {
	const out: Array<TabStopCandidate & { el: HTMLElement }> = [];
	for (const el of root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)) {
		const disabled = el.matches(':disabled');
		const tabIndex = el.tabIndex;
		const ariaHidden = el.closest('[aria-hidden="true"], [inert]') !== null;
		const rendered =
			typeof el.checkVisibility === 'function'
				? el.checkVisibility({ visibilityProperty: true })
				: el.getClientRects().length > 0;
		out.push({ el, disabled, tabIndex, ariaHidden, rendered });
	}
	return out;
}

export interface FocusTrapOptions {
	/** Selector inside the node, `'self'` for the node (needs tabindex="-1"), or omit for first tab stop. */
	initialFocus?: string;
}

type FocusTrapReturn = {
	update(next?: FocusTrapOptions): void;
	destroy(): void;
};

const trapStack: HTMLElement[] = [];
const trapListeners = new WeakMap<HTMLElement, (e: KeyboardEvent) => void>();

function isFocusableElement(el: unknown): el is HTMLElement {
	return (
		typeof el === 'object' &&
		el !== null &&
		'focus' in el &&
		typeof (el as HTMLElement).focus === 'function'
	);
}

function resolveInitialFocus(node: HTMLElement, options: FocusTrapOptions): HTMLElement {
	const spec = options.initialFocus;
	if (spec === 'self') return node;
	if (spec) {
		const picked = node.querySelector<HTMLElement>(spec);
		if (picked) return picked;
	}
	const first = nextTabStop(collectTabStops(node), null, false);
	if (first) return first.el;
	return node;
}

function isInForeignDialog(active: unknown, node: HTMLElement): boolean {
	if (active === null || active === document.body || !isFocusableElement(active)) return false;
	const foreign = active.closest('[role="dialog"], [role="alertdialog"]');
	if (foreign === null) return false;
	return foreign !== node && !node.contains(foreign);
}

function onTrapKeyDown(node: HTMLElement, e: KeyboardEvent): void {
	if (trapStack[trapStack.length - 1] !== node) return;
	if (e.key !== 'Tab' || e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;

	const active = document.activeElement;
	if (isInForeignDialog(active, node)) return;

	const stops = collectTabStops(node);
	const currentStop = stops.find((s) => s.el === active) ?? null;

	e.preventDefault();
	const next = nextTabStop(stops, currentStop, e.shiftKey);
	if (next === null) {
		node.focus();
	} else {
		next.el.focus();
	}
}

export function focusTrap(node: HTMLElement, options: FocusTrapOptions = {}): FocusTrapReturn {
	let opts = options;

	const rawActive = document.activeElement;
	let opener: HTMLElement | null = null;
	if (isFocusableElement(rawActive) && rawActive !== document.body && !node.contains(rawActive)) {
		opener = rawActive;
	}

	trapStack.push(node);
	const listener = (e: KeyboardEvent) => onTrapKeyDown(node, e);
	trapListeners.set(node, listener);
	document.addEventListener('keydown', listener);

	queueMicrotask(() => {
		if (!node.isConnected) return;
		const active = document.activeElement;
		if (active !== null && active !== document.body && node.contains(active)) return;
		resolveInitialFocus(node, opts).focus();
	});

	return {
		update(next?: FocusTrapOptions) {
			if (next !== undefined) opts = next;
		},
		destroy() {
			const registered = trapListeners.get(node);
			if (registered) {
				document.removeEventListener('keydown', registered);
				trapListeners.delete(node);
			}
			const idx = trapStack.indexOf(node);
			if (idx !== -1) trapStack.splice(idx, 1);

			const active = document.activeElement;
			const restore = active === null || active === document.body || node.contains(active);

			if (!restore) return;

			if (opener?.isConnected) {
				opener.focus();
				return;
			}
			if (
				isFocusableElement(active) &&
				active !== document.body &&
				typeof active.blur === 'function'
			) {
				active.blur();
			}
		}
	};
}
