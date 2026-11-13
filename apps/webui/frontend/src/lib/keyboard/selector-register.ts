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
 * #12539: the snapshot also drops a zero-area childless node (hidden or collapsed), and a node
 * whose children all drop. A node with children is visible through them, whatever its own rect
 * (a `display: contents` group can measure 0x0); a 0x0 survivor takes its children's union.
 * #12538: a parent cycle throws `SelectorParentCycleError` at resolve time, so every later
 * ancestor walk (here and in selector-session.ts) can trust that each chain ends at a root.
 *
 * `window.__odjSelector` discovers and invokes selector commands through the shared
 * command registry, using the same tree as its move/drill convenience methods.
 * `describeSelectorSelection` (#12267) is the JSON read model `__odjKeyboard.selector()` returns.
 * `onSelectorRegistryChange` (#12256) tells the live session a layout change removed nodes.
 * `selectorNodeElement` (#13428) finds a node's current element by id, for focus restoration.
 */
import {
	listPerformanceCommandMetadata,
	runCommand,
	SELECTOR_LEAF_COMMAND_IDS,
	type CommandMetadata,
	type PerformanceCommandId,
	type SelectorCommandId
} from './command-registry';
import { selectorLeafControlId } from './selector-activate';
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
/** Shipped deck-part leaves under each `deck.N` group (issue #12902 inventory). */
export const DECK_PARTS: readonly DeckPart[] = [
	'header',
	'cues',
	'loop',
	'transport',
	'jog',
	'pitch',
	'stems'
];
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

/** Stable ids for every declared deck part on one deck. */
export function deckPartNodeIds(deck: DeckId): readonly string[] {
	return DECK_PARTS.map((part) => selId.deckPart(deck, part));
}

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

/** #12538: thrown when selector parent links loop; the message names the cycle in walk order. */
export class SelectorParentCycleError extends Error {
	constructor(cycle: readonly string[]) {
		super(`Selector parent cycle: ${[...cycle, cycle[0]].join(' -> ')}`);
		this.name = 'SelectorParentCycleError';
	}
}

/** Every parent chain must reach a root; parentIds are already known to name a record. */
function _assertAcyclic(byId: Map<string, SelectorRecord>): void {
	const rooted = new Set<string>();
	for (const start of byId.values()) {
		const path: string[] = [];
		let cur: SelectorRecord | undefined = start;
		while (cur !== undefined && !rooted.has(cur.id)) {
			const seenAt = path.indexOf(cur.id);
			if (seenAt !== -1) throw new SelectorParentCycleError(path.slice(seenAt));
			path.push(cur.id);
			cur = cur.parentId === null ? undefined : byId.get(cur.parentId);
		}
		for (const id of path) rooted.add(id);
	}
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
	_assertAcyclic(byId);
	const isLive = (r: SelectorRecord): boolean => {
		for (let cur: SelectorRecord | undefined = r; cur;) {
			if (!cur.enabled) return false;
			cur = cur.parentId === null ? undefined : byId.get(cur.parentId);
		}
		return true;
	};
	const visibleRects = _visibleRects(records.filter(isLive));
	const live = records
		.filter((r) => visibleRects.has(r.id))
		.map((r) => ({ ...r, rect: visibleRects.get(r.id)! }));
	const roots = live.filter((r) => r.parentId === null).sort(_readingOrder);
	const rest = live.filter((r) => r.parentId !== null);
	return [...roots, ...rest].map(({ id, parentId, rect, kind }) => ({ id, parentId, rect, kind }));
}

const _hasArea = (rect: SelectorRect): boolean => rect.width > 0 && rect.height > 0;

/** #12539: the rect each visible record navigates by; hidden records are absent from the map. */
function _visibleRects(records: readonly SelectorRecord[]): Map<string, SelectorRect> {
	const children = new Map<string, SelectorRecord[]>();
	for (const r of records) {
		if (r.parentId !== null) children.set(r.parentId, [...(children.get(r.parentId) ?? []), r]);
	}
	const visible = new Map<string, SelectorRect>();
	const resolve = (r: SelectorRecord): SelectorRect | null => {
		const kids = children.get(r.id) ?? [];
		const kidRects = kids.map(resolve).filter((rect): rect is SelectorRect => rect !== null);
		let rect: SelectorRect | null;
		if (kids.length === 0) {
			rect = _hasArea(r.rect) ? r.rect : null;
		} else if (kidRects.length === 0) {
			rect = null;
		} else {
			// Only a 0x0 group takes the union: a zero-width edge column (the sampler) keeps its own rect.
			rect = r.rect.width === 0 && r.rect.height === 0 ? unionRect(kidRects) : r.rect;
		}
		if (rect !== null) visible.set(r.id, rect);
		return rect;
	};
	for (const r of records) if (r.parentId === null) resolve(r);
	return visible;
}

/** Tree commands only: the leaf commands (#13708) operate the live session's leaf, not a tree. */
function _selectorTreeCommands(): readonly CommandMetadata[] {
	const leafIds: readonly string[] = SELECTOR_LEAF_COMMAND_IDS;
	return listPerformanceCommandMetadata().filter(
		(command) => command.id.startsWith('selector.') && !leafIds.includes(command.id)
	);
}

/** Plain JSON, no DOM: `present: false` keeps a selected id whose node is no longer registered. */
export type SelectorStateSnapshot = {
	selectedId: string | null;
	present: boolean;
	kind: SelectorNodeKind | null;
	parentId: string | null;
	operations: PerformanceCommandId[];
};

function _isWithin(byId: Map<string, SelectorNode>, id: string, ancestorId: string): boolean {
	for (let cur = byId.get(id); cur !== undefined; cur = byId.get(cur.parentId ?? '')) {
		if (cur.id === ancestorId) return true;
	}
	return false;
}

/** The selection for agents: its id, kind, and every selector command that would act from it. */
export function describeSelectorSelection(
	nodes: readonly SelectorNode[],
	selectedId: string | null
): SelectorStateSnapshot {
	const byId = new Map(nodes.map((node) => [node.id, node]));
	const node = selectedId === null ? undefined : byId.get(selectedId);
	if (selectedId === null || node === undefined) {
		return { selectedId, present: false, kind: null, parentId: null, operations: [] };
	}
	// A tree command acts when a fresh tree on this node selects something else.
	const treeOps = _selectorTreeCommands()
		.map((command) => command.id)
		.filter((id) => {
			const selector = createSelectorTree(nodes, selectedId);
			return runCommand(id, { timeStamp: 0, selector }) !== selectedId;
		});
	// Leaf commands mirror the keydown router: library rows keep Enter, arrows need a dial or fader.
	let leafOps: PerformanceCommandId[];
	if (node.kind !== 'leaf' || _isWithin(byId, selectedId, selId.section('library'))) {
		leafOps = [];
	} else if (selectorLeafControlId(selectedId) === null) {
		leafOps = ['selector.activate'];
	} else {
		leafOps = [...SELECTOR_LEAF_COMMAND_IDS];
	}
	return {
		selectedId,
		present: true,
		kind: node.kind,
		parentId: node.parentId,
		operations: [...treeOps, ...leafOps]
	};
}

//----- DOM shell

const registry = new Map<HTMLElement, SelectorNodeParams>();
const changeListeners = new Set<() => void>();
let registryVersion = 0;
let changeQueued = false;

export function selectorRegistryVersion(): number {
	return registryVersion;
}

/** #12256: call `listener` once after each batch of node registrations, updates and removals. */
export function onSelectorRegistryChange(listener: () => void): () => void {
	changeListeners.add(listener);
	return () => changeListeners.delete(listener);
}

function _registryChanged(): void {
	registryVersion++;
	if (changeQueued) return;
	changeQueued = true;
	// One microtask per batch: a layout change unmounts or disables many nodes in one flush.
	queueMicrotask(() => {
		changeQueued = false;
		for (const listener of [...changeListeners]) listener();
	});
}

function _sameParams(a: SelectorNodeParams, b: SelectorNodeParams): boolean {
	return a.id === b.id && a.kind === b.kind && a.enabled === b.enabled;
}

function _applyAttrs(el: HTMLElement, p: SelectorNodeParams): void {
	el.setAttribute('data-sel-node', p.id);
	el.setAttribute('data-sel-kind', p.kind);
	el.toggleAttribute('data-sel-disabled', !p.enabled);
}

export function selectorNode(el: HTMLElement, params: SelectorNodeParams) {
	registry.set(el, params);
	_applyAttrs(el, params);
	_registryChanged();
	return {
		update(next: SelectorNodeParams): void {
			const previous = registry.get(el);
			registry.set(el, next);
			_applyAttrs(el, next);
			if (previous === undefined || !_sameParams(previous, next)) _registryChanged();
		},
		destroy(): void {
			registry.delete(el);
			el.removeAttribute('data-sel-node');
			el.removeAttribute('data-sel-kind');
			el.removeAttribute('data-sel-disabled');
			_registryChanged();
		}
	};
}

/** #13428: the element now registered for node `id`, or null when no mounted element carries it. */
export function selectorNodeElement(id: string): HTMLElement | null {
	for (const [el, p] of registry) if (p.id === id) return el;
	return null;
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

/**
 * Returns a getter for a tree that rebuilds from a fresh snapshot whenever the registry
 * version moves (mount, unmount, enabled change), so moves never walk stale records. The
 * selection survives when its node does, else falls back to its nearest surviving ancestor,
 * else the first node.
 */
export function createLiveSelectorTree(
	snapshot: () => readonly SelectorNode[],
	version: () => number,
	initialId?: string
): () => SelectorTree {
	let nodes: readonly SelectorNode[] = [];
	let builtAt = version();
	const build = (pick: (nodes: readonly SelectorNode[]) => string | undefined): SelectorTree => {
		const fresh = snapshot();
		const first = fresh[0];
		if (first === undefined) throw new Error('No selector nodes registered');
		nodes = fresh;
		builtAt = version();
		return createSelectorTree(fresh, pick(fresh) ?? first.id);
	};
	let tree = build(() => initialId);
	return (): SelectorTree => {
		if (version() === builtAt) return tree;
		const prior = new Map(nodes.map((n) => [n.id, n]));
		const selectedId = tree.selected();
		tree = build((fresh) => {
			const ids = new Set(fresh.map((n) => n.id));
			for (let cur = prior.get(selectedId); cur;) {
				if (ids.has(cur.id)) return cur.id;
				cur = cur.parentId === null ? undefined : prior.get(cur.parentId);
			}
			return undefined;
		});
		return tree;
	};
}

export type SelectorDebugHandle = {
	commands(): readonly CommandMetadata[];
	run(id: SelectorCommandId): string;
	nodes(): SelectorNode[];
	deckParts(): readonly DeckPart[];
	deckPartNodeIds(deck: DeckId): readonly string[];
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
	let tree: (() => SelectorTree) | null = null;
	const live = (): SelectorTree => {
		if (tree === null) throw new Error('__odjSelector.start() must run first');
		return tree();
	};
	const commands = _selectorTreeCommands;
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
		deckParts: () => DECK_PARTS,
		deckPartNodeIds,
		start(initialId?: string): string {
			tree = createLiveSelectorTree(snapshotSelectorNodes, selectorRegistryVersion, initialId);
			return tree().selected();
		},
		selected: () => live().selected(),
		move: (dir) => dispatch(`selector.move.${dir}`),
		drillIn: () => dispatch('selector.drill-in'),
		drillOut: () => dispatch('selector.drill-out')
	};
}
