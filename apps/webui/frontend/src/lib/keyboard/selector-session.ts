/**
 * The one live keyboard selector on /performance (#9395, K3 of epic #9372).
 *
 * The tree is built lazily on the first selector key from a snapshot of the registered
 * nodes, and rebuilt (keeping the selection when its node survives) whenever the node
 * registry version has moved since. The first key from a cleared session only reveals the
 * start node, so the operator sees the selector before it travels. Every selection is
 * published to the ring; `clear` (Esc) drops the tree and hides the ring.
 * `commandHost` (#13770) hands `window.__odjKeyboard` the same session the keys drive.
 * #12256: when a layout change removes the selected node (unmounted, or deck 3/4 disabled in
 * two-deck mode) the selection moves to its nearest surviving ancestor, else the first root,
 * and the ring follows. `reconcile` does that eagerly; every read and key does it lazily.
 */
import { runCommand, type SelectorCommandId } from './command-registry';
import type { SelectorRingTarget } from './selector-ring';
import {
	createSelectorTree,
	type SelectorDirection,
	type SelectorNode,
	type SelectorTree
} from './selector-tree';

/** The move and drill surface a selector command runs against, plus the selection it reads. */
type SelectorCommandHost = Pick<SelectorTree, 'move' | 'drillIn' | 'drillOut'> & {
	selected(): string | null;
};

export interface SelectorSessionDeps {
	snapshot: () => SelectorNode[];
	version: () => number;
	showRing: (target: SelectorRingTarget) => void;
	hideRing: () => void;
}

export interface SelectorSession {
	selected(): string | null;
	/** #13708: the selected id when it is a leaf (Enter and the arrows operate it), else null. */
	selectedLeaf(): string | null;
	selectionWithin(ancestorId: string): boolean;
	run(command: SelectorCommandId, opts: { timeStamp: number; startId: string | null }): string;
	clear(): boolean;
	/** #12256: move a selection whose node left the registry; the surviving selection, or null. */
	reconcile(): string | null;
	/** #13770: move and drill through this session, exactly as a selector key press does. */
	commandHost(clock: () => number): SelectorCommandHost;
}

/** The first of `id` and its ancestors (read from the old snapshot) still in `live`, else null. */
function _nearestSurvivor(
	previous: Map<string, SelectorNode>,
	id: string,
	live: Map<string, SelectorNode>
): string | null {
	for (let cur: string | null = id; cur !== null; cur = previous.get(cur)?.parentId ?? null) {
		if (live.has(cur)) return cur;
	}
	return null;
}

/** One selector session: lazy tree, registry-version rebuild, ring kept in step. */
export function createSelectorSession(deps: SelectorSessionDeps): SelectorSession {
	let tree: SelectorTree | null = null;
	let byId = new Map<string, SelectorNode>();
	let builtVersion = -1;

	function _treeOver(
		nodes: SelectorNode[],
		first: SelectorNode,
		preferredId: string | null
	): SelectorTree {
		byId = new Map(nodes.map((node) => [node.id, node]));
		builtVersion = deps.version();
		const initialId = preferredId !== null && byId.has(preferredId) ? preferredId : first.id;
		return createSelectorTree(nodes, initialId);
	}

	function _build(preferredId: string | null): SelectorTree {
		const nodes = deps.snapshot();
		const first = nodes[0];
		if (first === undefined)
			throw new Error('Selector key pressed but no selector nodes are registered');
		return _treeOver(nodes, first, preferredId);
	}

	/** Rebuild a stale tree off any removed selection; true when it rebuilt, cleared when empty. */
	function _rebuildIfStale(): boolean {
		if (tree === null || deps.version() === builtVersion) return false;
		const nodes = deps.snapshot();
		const first = nodes[0];
		if (first === undefined) {
			session.clear();
			return false;
		}
		const live = new Map(nodes.map((node) => [node.id, node]));
		tree = _treeOver(nodes, first, _nearestSurvivor(byId, tree.selected(), live));
		return true;
	}

	function _sync(): string | null {
		if (_rebuildIfStale() && tree !== null) _publish(tree);
		return tree?.selected() ?? null;
	}

	function _publish(live: SelectorTree): string {
		const id = live.selected();
		const node = byId.get(id);
		if (node === undefined) throw new Error(`Selected selector node ${id} is not in the snapshot`);
		deps.showRing(node);
		return id;
	}

	const session: SelectorSession = {
		selected: _sync,
		selectedLeaf(): string | null {
			const id = _sync();
			return id !== null && byId.get(id)?.kind === 'leaf' ? id : null;
		},
		selectionWithin(ancestorId: string): boolean {
			for (let id = _sync(); id !== null;) {
				if (id === ancestorId) return true;
				id = byId.get(id)?.parentId ?? null;
			}
			return false;
		},
		run(command, { timeStamp, startId }): string {
			if (tree === null) {
				tree = _build(startId);
				return _publish(tree);
			}
			_rebuildIfStale();
			if (tree === null) return session.run(command, { timeStamp, startId });
			runCommand(command, { timeStamp, selector: tree });
			return _publish(tree);
		},
		clear(): boolean {
			if (tree === null) return false;
			tree = null;
			deps.hideRing();
			return true;
		},
		reconcile: _sync,
		commandHost(clock: () => number): SelectorCommandHost {
			const press = (command: SelectorCommandId): string =>
				session.run(command, { timeStamp: clock(), startId: null });
			return {
				selected: session.selected,
				move: (direction: SelectorDirection) => press(`selector.move.${direction}`),
				drillIn: () => press('selector.drill-in'),
				drillOut: () => press('selector.drill-out')
			};
		}
	};
	return session;
}
