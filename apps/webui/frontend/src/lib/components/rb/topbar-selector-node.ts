/**
 * Keyboard selector nodes for the /performance topbar controls (GitHub issue #13700, epic #9372).
 *
 * Each control is a leaf under the `topbar` section, with a plain string id
 * `topbar.<name>`, so `selector-register.ts` gains no new helper. The topbar hides
 * controls through width and LESS-view CSS tiers (display: none), so a leaf's enabled
 * flag follows whether it renders: a ResizeObserver re-registers it when a tier flips,
 * and the snapshot then drops it. A disabled control is not a live node either.
 * `areaTrackedSelectorLeaf` is that mechanism with any id, so a control mounted outside the
 * topbar (the waveform area's sampler chip, `waveforms.sampler`) reuses it.
 */
import { selId, selectorNode, type SelectorNodeParams } from '$lib/keyboard/selector-register';
import type { SelectorRect } from '$lib/keyboard/selector-tree.ts';

/** Topbar selector leaves in DOM (left to right) order. */
export const TOPBAR_SELECTOR_NAMES = [
	'analysis-source',
	'mode',
	'posture',
	'recorder',
	'layout',
	'feedback',
	'thumbs',
	'pairing',
	'beat-sync-max',
	'lyrics',
	'autoplay',
	'pads',
	'jobs',
	'command',
	'track-info',
	'oj',
	'set-notes',
	'skin',
	'wave-settings',
	'settings',
	'refresh',
	'master',
	'mute',
	'account'
] as const;

export type TopbarSelectorName = (typeof TOPBAR_SELECTOR_NAMES)[number];
export type TopbarSelectorParams = { name: TopbarSelectorName; disabled: boolean };

/** Returns the selector id of a topbar control, `topbar.<name>`. */
export function topbarSelId(name: TopbarSelectorName): string {
	return `${selId.section('topbar')}.${name}`;
}

/** True when a topbar control is a live node: it renders with area and is not disabled. */
export function topbarNodeEnabled(rect: SelectorRect, disabled: boolean): boolean {
	return !disabled && rect.width > 0 && rect.height > 0;
}

export type AreaTrackedLeafParams = { id: string; disabled: boolean };

/** Registers a selector leaf whose enabled flag tracks rendering with area and disabled. */
export function areaTrackedSelectorLeaf(el: HTMLElement, params: AreaTrackedLeafParams) {
	let current = params;
	const nodeParams = (): SelectorNodeParams => {
		const r = el.getBoundingClientRect();
		const rect = { x: r.x, y: r.y, width: r.width, height: r.height };
		return { id: current.id, kind: 'leaf', enabled: topbarNodeEnabled(rect, current.disabled) };
	};
	let registered = nodeParams();
	const node = selectorNode(el, registered);
	const refresh = (): void => {
		const next = nodeParams();
		if (next.id === registered.id && next.enabled === registered.enabled) return;
		registered = next;
		node.update(next);
	};
	const resize = new ResizeObserver(refresh);
	resize.observe(el);
	return {
		update(next: AreaTrackedLeafParams): void {
			current = next;
			refresh();
		},
		destroy(): void {
			resize.disconnect();
			node.destroy();
		}
	};
}

/** Registers a topbar control as a selector leaf whose enabled flag tracks rendering and disabled. */
export function topbarSelectorNode(el: HTMLElement, params: TopbarSelectorParams) {
	const leafParams = (p: TopbarSelectorParams): AreaTrackedLeafParams => ({
		id: topbarSelId(p.name),
		disabled: p.disabled
	});
	const leaf = areaTrackedSelectorLeaf(el, leafParams(params));
	return {
		update(next: TopbarSelectorParams): void {
			leaf.update(leafParams(next));
		},
		destroy(): void {
			leaf.destroy();
		}
	};
}
