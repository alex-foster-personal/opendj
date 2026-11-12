/**
 * Selector node registration for /performance (GitHub issue #9397, K5 of epic #9372).
 *
 * Components mark nodes with the `selectorNode` action, which sets `data-sel-node` and
 * `data-sel-kind` and records the element. A node's parent is its nearest DOM ancestor
 * carrying `data-sel-node`, so no component passes parent ids around. Rects are measured
 * only when a snapshot is taken (mount order and the deck LESS/MORE transition make
 * register-time rects wrong); a `display: contents` wrapper measures as the union of its
 * children. A disabled node (decks 3/4 in two-deck mode) keeps its attribute, gains
 * `data-sel-disabled`, and the snapshot drops it with its whole subtree.
 *
 * `window.__odjSelector` discovers and invokes selector commands through the shared
 * command registry, using the same tree as its move/drill convenience methods.
 */
import {
	listPerformanceCommandMetadata,
	runCommand,
	type CommandMetadata,
	type SelectorCommandId
} from './command-registry';
import {
	createSelectorTree,
	type SelectorDirection,
	type SelectorNode,
	type SelectorNodeKind,
	type SelectorRect,
	type SelectorTree
} from './selector-tree.ts';
import type { DeckId } from '$lib/rb/deck-slots';
import type { StemControl } from '$lib/rb/stem-types';

export type SectionId = 'topbar' | 'waveforms' | 'decks' | 'library';
export type DeckPart = 'header' | 'cues' | 'loop' | 'transport' | 'jog' | 'pitch' | 'stems';
export type ChannelPart = 'trim' | 'high' | 'mid' | 'low' | 'filter' | 'fader';
export type SelectorNodeParams = { id: string; kind: SelectorNodeKind; enabled: boolean };
export type SelectorRecord = SelectorNodeParams & { parentId: string | null; rect: SelectorRect };

export const selId = {
	section: (s: SectionId): string => s,
	deck: (n: DeckId): string => `deck.${n}`,
	deckPart: (n: DeckId, p: DeckPart): string => `deck.${n}.${p}`,
	mixer: (): string => 'mixer',
	channel: (n: DeckId): string => `mixer.ch${n}`,
	channelPart: (n: DeckId, p: ChannelPart): string => `mixer.ch${n}.${p}`,
	channelStem: (n: DeckId, stem: StemControl): string => `mixer.ch${n}.stem.${stem}`,
	crossfader: (): string => 'mixer.crossfader',
	headphones: (): string => 'mixer.headphones'
} as const;

//----- pure core

const ZERO_RECT: SelectorRect = { x: 0, y: 0, width: 0, height: 0 };

export function unionRect(rects: readonly SelectorRect[]): SelectorRect {
	const live = rects.filter((r) => r.width > 0 && r.height > 0);
	if (live.length === 0) return { ...ZERO_RECT };
	const left = Math.min(...live.map((r) => r.x));
	const top = Math.min(...live.map((r) => r.y));
	const right = Math.max(...live.map((r) => r.x + r.width));
	const bottom = Math.max(...live.map((r) => r.y + r.height));
	return { x: left, y: top, width: right - left, height: bottom - top };
}

function _readingOrder(a: SelectorRecord, b: SelectorRecord): number {
	return a.rect.y - b.rect.y || a.rect.x - b.rect.x || (a.id < b.id ? -1 : 1);
}

/** Validates records, drops disabled subtrees, returns roots in reading order then the rest. */
export function resolveSelectorNodes(records: readonly SelectorRecord[]): SelectorNode[] {
	const byId = new Map<string, SelectorRecord>();
	for (const r of records) {
		if (byId.has(r.id)) throw new Error(`Duplicate selector node id: ${r.id}`);
		byId.set(r.id, r);
	}
	for (const r of records) {
		if (r.parentId !== null && !byId.has(r.parentId)) {
			throw new Error(`Unknown parentId on selector node ${r.id}: ${r.parentId}`);
		}
	}
	const isLive = (r: SelectorRecord): boolean => {
		for (let cur: SelectorRecord | undefined = r; cur;) {
			if (!cur.enabled) return false;
			cur = cur.parentId === null ? undefined : byId.get(cur.parentId);
		}
		return true;
	};
	const live = records.filter(isLive);
	const roots = live.filter((r) => r.parentId === null).sort(_readingOrder);
	const rest = live.filter((r) => r.parentId !== null);
	return [...roots, ...rest].map(({ id, parentId, rect, kind }) => ({ id, parentId, rect, kind }));
}

//----- DOM shell

const registry = new Map<HTMLElement, SelectorNodeParams>();
let registryVersion = 0;

export function selectorRegistryVersion(): number {
	return registryVersion;
}

function _applyAttrs(el: HTMLElement, p: SelectorNodeParams): void {
	el.setAttribute('data-sel-node', p.id);
	el.setAttribute('data-sel-kind', p.kind);
	el.toggleAttribute('data-sel-disabled', !p.enabled);
}

export function selectorNode(el: HTMLElement, params: SelectorNodeParams) {
	registry.set(el, params);
	_applyAttrs(el, params);
	registryVersion++;
	return {
		update(next: SelectorNodeParams): void {
			registry.set(el, next);
			_applyAttrs(el, next);
			registryVersion++;
		},
		destroy(): void {
			registry.delete(el);
			el.removeAttribute('data-sel-node');
			el.removeAttribute('data-sel-kind');
			el.removeAttribute('data-sel-disabled');
			registryVersion++;
		}
	};
}

function _measureRect(el: Element): SelectorRect {
	if (getComputedStyle(el).display === 'contents') {
		return unionRect(Array.from(el.children, _measureRect));
	}
	const r = el.getBoundingClientRect();
	return { x: r.x, y: r.y, width: r.width, height: r.height };
}

export function snapshotSelectorNodes(): SelectorNode[] {
	const records: SelectorRecord[] = [];
	for (const [el, p] of registry) {
		const parentEl = el.parentElement?.closest<HTMLElement>('[data-sel-node]') ?? null;
		if (parentEl !== null && !registry.has(parentEl)) {
			throw new Error(`Selector node ${p.id} sits under an unregistered data-sel-node element`);
		}
		const parentId = parentEl === null ? null : registry.get(parentEl)!.id;
		records.push({ ...p, parentId, rect: _measureRect(el) });
	}
	return resolveSelectorNodes(records);
}

export function buildSelectorTree(initialId?: string): SelectorTree {
	const nodes = snapshotSelectorNodes();
	const first = nodes[0];
	if (first === undefined) throw new Error('No selector nodes registered');
	return createSelectorTree(nodes, initialId ?? first.id);
}

export type SelectorDebugHandle = {
	commands(): readonly CommandMetadata[];
	run(id: SelectorCommandId): string;
	nodes(): SelectorNode[];
	start(initialId?: string): string;
	selected(): string;
	move(dir: SelectorDirection): string;
	drillIn(): string;
	drillOut(): string;
};

declare global {
	interface Window {
		__odjSelector?: SelectorDebugHandle;
	}
}

export function installSelectorDebugHandle(): void {
	let tree: SelectorTree | null = null;
	const live = (): SelectorTree => {
		if (tree === null) throw new Error('__odjSelector.start() must run first');
		return tree;
	};
	const commands = (): readonly CommandMetadata[] =>
		listPerformanceCommandMetadata().filter((command) => command.id.startsWith('selector.'));
	const dispatch = (id: SelectorCommandId): string => {
		if (!commands().some((command) => command.id === id)) {
			throw new Error(`Unknown selector command: ${id}`);
		}
		const selectedId = runCommand(id, { timeStamp: performance.now(), selector: live() });
		if (typeof selectedId !== 'string') {
			throw new Error(`Selector command ${id} did not return a selected node id`);
		}
		return selectedId;
	};
	window.__odjSelector = {
		commands,
		run: dispatch,
		nodes: snapshotSelectorNodes,
		start(initialId?: string): string {
			tree = buildSelectorTree(initialId);
			return tree.selected();
		},
		selected: () => live().selected(),
		move: (dir) => dispatch(`selector.move.${dir}`),
		drillIn: () => dispatch('selector.drill-in'),
		drillOut: () => dispatch('selector.drill-out')
	};
}
