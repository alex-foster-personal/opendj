import type { Page } from '@playwright/test';

/** Vite dev URL so in-page `import()` can reach selector ring writers (midi-maps pattern). */
export const SELECTOR_RING_STATE_URL = '/src/lib/keyboard/selector-ring-state.svelte.ts';

export type FocusSelectorCoexistence = {
	focus: {
		outlineStyle: string;
		outlineWidth: string;
		outlineColor: string;
		focusVisible: boolean;
		selNodeId: string | null;
	} | null;
	selectorRing: { borderColor: string; borderWidth: string } | null;
};

export async function syncSelectorRingToDomSelection(page: Page): Promise<string> {
	return page.evaluate(async (url) => {
		const sel = window.__odjSelector;
		if (sel === undefined) throw new Error('__odjSelector is not installed');
		const id = sel.selected();
		const el = document.querySelector<HTMLElement>(`[data-sel-node="${CSS.escape(id)}"]`);
		if (el === null) throw new Error(`Missing [data-sel-node="${id}"]`);
		const kind = el.getAttribute('data-sel-kind');
		if (kind !== 'section' && kind !== 'group' && kind !== 'leaf') {
			throw new Error(`Unexpected data-sel-kind on ${id}: ${kind}`);
		}
		const r = el.getBoundingClientRect();
		const mod = (await import(url)) as typeof import('$lib/keyboard/selector-ring-state.svelte');
		mod.setSelectorRingTarget({
			id,
			kind,
			rect: { x: r.x, y: r.y, width: r.width, height: r.height }
		});
		return id;
	}, SELECTOR_RING_STATE_URL);
}

export async function syncSelectorRingToNodeId(page: Page, nodeId: string): Promise<void> {
	await page.evaluate(
		async ({ url, id }) => {
			const el = document.querySelector<HTMLElement>(`[data-sel-node="${CSS.escape(id)}"]`);
			if (el === null) throw new Error(`Missing [data-sel-node="${id}"]`);
			const kind = el.getAttribute('data-sel-kind');
			if (kind !== 'section' && kind !== 'group' && kind !== 'leaf') {
				throw new Error(`Unexpected data-sel-kind on ${id}: ${kind}`);
			}
			const r = el.getBoundingClientRect();
			const mod = (await import(url)) as typeof import('$lib/keyboard/selector-ring-state.svelte');
			mod.setSelectorRingTarget({
				id,
				kind,
				rect: { x: r.x, y: r.y, width: r.width, height: r.height }
			});
		},
		{ url: SELECTOR_RING_STATE_URL, id: nodeId }
	);
}

export function readFocusSelectorCoexistence(page: Page): Promise<FocusSelectorCoexistence> {
	return page.evaluate(() => {
		const el = document.activeElement as HTMLElement | null;
		const ring = document.querySelector<HTMLElement>('[data-testid="selector-ring"]');
		const selNodeId = el?.closest('[data-sel-node]')?.getAttribute('data-sel-node') ?? null;
		const focus =
			el !== null && el !== document.body
				? {
						outlineStyle: getComputedStyle(el).outlineStyle,
						outlineWidth: getComputedStyle(el).outlineWidth,
						outlineColor: getComputedStyle(el).outlineColor,
						focusVisible: el.matches(':focus-visible'),
						selNodeId
					}
				: null;
		const selectorRing =
			ring !== null
				? {
						borderColor: getComputedStyle(ring).borderTopColor,
						borderWidth: getComputedStyle(ring).borderTopWidth
					}
				: null;
		return { focus, selectorRing };
	});
}

/** Selector ring border must stay visibly different from the DOM focus outline. */
export function assertFocusAndSelectorRingsDistinguishable(
	snapshot: FocusSelectorCoexistence,
	context: string
): void {
	const { focus, selectorRing } = snapshot;
	if (focus === null) throw new Error(`${context}: no focused element`);
	if (selectorRing === null) throw new Error(`${context}: selector ring is not rendered`);
	if (!focus.focusVisible) {
		throw new Error(`${context}: expected :focus-visible on the tab stop`);
	}
	if (focus.outlineStyle === 'none') {
		throw new Error(`${context}: focus outline was removed while the selector ring is visible`);
	}
	const outlinePx = Number.parseFloat(focus.outlineWidth);
	if (!Number.isFinite(outlinePx) || outlinePx < 2) {
		throw new Error(`${context}: focus outline under 2px with selector ring present`);
	}
	if (focus.outlineColor === selectorRing.borderColor) {
		throw new Error(`${context}: focus outline color matches the selector ring accent border`);
	}
}
