/**
 * Pure selector tree and spatial navigation core (GitHub issue #9393, K1).
 *
 * Distance scoring follows the W3C CSS Spatial Navigation draft (css-nav-1),
 * section 8: https://drafts.csswg.org/css-nav-1/#processing-model
 *
 * No runtime imports: safe for strip-types unit tests and future UI wiring.
 */
export type SelectorDirection = 'up' | 'down' | 'left' | 'right';
export type SelectorNodeKind = 'section' | 'group' | 'leaf';
export type SelectorRect = { x: number; y: number; width: number; height: number };
export type SelectorNode = {
	id: string;
	parentId: string | null;
	rect: SelectorRect;
	kind: SelectorNodeKind;
};
export type SelectorTree = {
	selected(): string;
	move(dir: SelectorDirection): string;
	drillIn(): string;
	drillOut(): string;
};

function _edges(r: SelectorRect): { left: number; right: number; top: number; bottom: number } {
	return { left: r.x, right: r.x + r.width, top: r.y, bottom: r.y + r.height };
}

/**
 * Returns null when the candidate rect is not in the move half-plane from `from`.
 * Otherwise returns primary + orthogonal cost per css-nav-1 section 8 reading:
 * euclidean distance between nearest edges plus twice the orthogonal offset.
 */
export function spatialScore(
	from: SelectorRect,
	to: SelectorRect,
	dir: SelectorDirection
): number | null {
	const a = _edges(from);
	const b = _edges(to);
	let primary: number;
	let orthogonal: number;
	switch (dir) {
		case 'right':
			if (b.left < a.right) return null;
			primary = b.left - a.right;
			orthogonal = Math.max(0, b.top - a.bottom, a.top - b.bottom);
			break;
		case 'left':
			if (b.right > a.left) return null;
			primary = a.left - b.right;
			orthogonal = Math.max(0, b.top - a.bottom, a.top - b.bottom);
			break;
		case 'down':
			if (b.top < a.bottom) return null;
			primary = b.top - a.bottom;
			orthogonal = Math.max(0, b.left - a.right, a.left - b.right);
			break;
		case 'up':
			if (b.bottom > a.top) return null;
			primary = a.top - b.bottom;
			orthogonal = Math.max(0, b.left - a.right, a.left - b.right);
			break;
		default: {
			const _exhaustive: never = dir;
			throw new Error(`Unhandled direction: ${_exhaustive}`);
		}
	}
	return Math.hypot(primary, orthogonal) + 2 * orthogonal;
}

export function pickNeighbour(
	from: SelectorNode,
	candidates: readonly SelectorNode[],
	dir: SelectorDirection
): SelectorNode | null {
	let best: SelectorNode | null = null;
	let bestScore = Number.POSITIVE_INFINITY;
	for (const candidate of candidates) {
		if (candidate.id === from.id) continue;
		const score = spatialScore(from.rect, candidate.rect, dir);
		if (score === null) continue;
		if (score < bestScore) {
			bestScore = score;
			best = candidate;
			continue;
		}
		if (score === bestScore && best !== null && candidate.id < best.id) {
			best = candidate;
		}
	}
	return best;
}

function _topmostChild(children: readonly SelectorNode[]): SelectorNode {
	let pick = children[0];
	for (let i = 1; i < children.length; i++) {
		const c = children[i];
		if (c.rect.y < pick.rect.y) {
			pick = c;
			continue;
		}
		if (c.rect.y === pick.rect.y && c.rect.x < pick.rect.x) {
			pick = c;
			continue;
		}
		if (c.rect.y === pick.rect.y && c.rect.x === pick.rect.x && c.id < pick.id) {
			pick = c;
		}
	}
	return pick;
}

/** Rejects NaN, infinite or negative-size rects so spatial scoring stays deterministic (#12542). */
function _assertValidRect(node: SelectorNode): void {
	const { x, y, width, height } = node.rect;
	for (const [name, value] of [
		['x', x],
		['y', y],
		['width', width],
		['height', height]
	] as const) {
		if (!Number.isFinite(value)) {
			throw new Error(`Non-finite rect.${name} on selector node ${node.id}: ${value}`);
		}
	}
	if (width < 0 || height < 0) {
		throw new Error(`Negative rect size on selector node ${node.id}: ${width}x${height}`);
	}
}

/**
 * Reading order: the first row is every child whose top sits above the topmost child's
 * bottom edge, so a centred row of mixed-height controls (the topbar) starts at its left
 * end rather than at its tallest control (#13700). The leftmost of that row wins.
 */
function _topLeftChild(children: readonly SelectorNode[]): SelectorNode {
	const top = _topmostChild(children);
	const rowBottom = top.rect.y + top.rect.height;
	let pick = top;
	for (const c of children) {
		if (c === top || c.rect.y >= rowBottom) continue;
		if (c.rect.x < pick.rect.x) {
			pick = c;
			continue;
		}
		if (c.rect.x === pick.rect.x && c.rect.y < pick.rect.y) {
			pick = c;
		}
	}
	return pick;
}

export function createSelectorTree(
	nodes: readonly SelectorNode[],
	initialId: string
): SelectorTree {
	const byId = new Map<string, SelectorNode>();
	for (const node of nodes) {
		_assertValidRect(node);
		if (byId.has(node.id)) {
			throw new Error(`Duplicate selector node id: ${node.id}`);
		}
		byId.set(node.id, node);
	}
	for (const node of nodes) {
		if (node.parentId === null) continue;
		const parent = byId.get(node.parentId);
		if (parent === undefined) {
			throw new Error(`Unknown parentId on node ${node.id}: ${node.parentId}`);
		}
		if (parent.kind === 'leaf') {
			throw new Error(`Node ${node.id} may not parent under leaf ${parent.id}`);
		}
	}
	if (!byId.has(initialId)) {
		throw new Error(`Unknown initial selection id: ${initialId}`);
	}

	const children = new Map<string | null, SelectorNode[]>();
	for (const node of nodes) {
		const key = node.parentId;
		const list = children.get(key);
		if (list === undefined) {
			children.set(key, [node]);
		} else {
			list.push(node);
		}
	}

	let selectedId = initialId;
	const lastVisited = new Map<string, string>();

	function select(id: string): void {
		selectedId = id;
		const node = byId.get(id);
		if (node === undefined) return;
		if (node.parentId !== null) {
			lastVisited.set(node.parentId, id);
		}
	}

	select(initialId);

	return {
		selected(): string {
			return selectedId;
		},
		move(dir: SelectorDirection): string {
			const current = byId.get(selectedId);
			if (current === undefined) return selectedId;
			const siblings = children.get(current.parentId);
			if (siblings === undefined || siblings.length === 0) return selectedId;
			const next = pickNeighbour(current, siblings, dir);
			if (next === null) return selectedId;
			select(next.id);
			return selectedId;
		},
		drillIn(): string {
			const kids = children.get(selectedId);
			if (kids === undefined || kids.length === 0) return selectedId;
			const preferred = lastVisited.get(selectedId);
			let target: SelectorNode | null = null;
			if (preferred !== undefined) {
				target = kids.find((c) => c.id === preferred) ?? null;
			}
			if (target === null) {
				target = _topLeftChild(kids);
			}
			select(target.id);
			return selectedId;
		},
		drillOut(): string {
			const current = byId.get(selectedId);
			if (current === undefined) return selectedId;
			if (current.parentId === null) return selectedId;
			select(current.parentId);
			return selectedId;
		}
	};
}
