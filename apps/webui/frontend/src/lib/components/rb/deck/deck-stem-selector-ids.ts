/**
 * Selector node ids for a deck's stem row (#14471, epic #9372).
 *
 * `deck.<n>.stems` is a group holding one leaf per StemRow button: each stem mute chip,
 * each quick stem mode, the track-load reset chip, and the stem load and separate actions
 * when they show. StemRow registers these only when its parent passes a selector prefix;
 * the mixer channel strip passes none (it registers its own stem dial leaves), so its
 * StemRow adds no nodes. Ids compose under the prefix, so selector-register.ts needs no
 * new builder.
 */
import { selectorNode, type SelectorNodeParams } from '$lib/keyboard/selector-register';
import type { SelectorNodeKind } from '$lib/keyboard/selector-tree';
import type { StemQuickMode } from '$lib/rb/stem-quick-mode';
import type { StemControl } from '$lib/rb/stem-types';

export type StemRowSelPart = StemControl | `quick.${StemQuickMode}` | 'reset' | 'load' | 'separate';

/** Kind of the deck's stems part: a group over the Svelte row's leaves, one leaf around the React twin (it registers none). */
export function deckStemsPartKind(reactDeck: boolean): SelectorNodeKind {
	return reactDeck ? 'leaf' : 'group';
}

/** Leaf id of one StemRow button under the selector prefix its parent passed. */
export function stemRowSelId(prefix: string, part: StemRowSelPart): string {
	return `${prefix}.${part}`;
}

/** Leaf params for one StemRow button, or null when the row has no selector prefix. */
export function stemRowLeaf(
	prefix: string | null,
	part: StemRowSelPart,
	enabled: boolean
): SelectorNodeParams | null {
	return prefix === null ? null : { id: stemRowSelId(prefix, part), kind: 'leaf', enabled };
}

/** The selectorNode action when params are given; null registers nothing on the element. */
export function optionalSelectorNode(el: HTMLElement, params: SelectorNodeParams | null) {
	let node = params === null ? null : selectorNode(el, params);
	return {
		update(next: SelectorNodeParams | null): void {
			if (next === null) {
				node?.destroy();
				node = null;
			} else if (node === null) {
				node = selectorNode(el, next);
			} else {
				node.update(next);
			}
		},
		destroy(): void {
			node?.destroy();
			node = null;
		}
	};
}
