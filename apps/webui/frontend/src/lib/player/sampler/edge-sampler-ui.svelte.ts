/**
 * Minimal edge-sampler UI state for slice S6 (motion, paging, held tabs).
 * Sample playback and catalogue wiring land in later slices.
 */

export type EdgeId = 'L' | 'R';

const HOT_CUE_COLORS = [
	'#e85d4f',
	'#4fa3e8',
	'#5cb85c',
	'#f0ad4e',
	'#9b59b6',
	'#1abc9c',
	'#e67e22',
	'#3498db',
	'#2ecc71',
	'#e91e63'
];

export interface EdgeTabStub {
	index: number;
	/** false = hidden at rest */
	filled: boolean;
	color: string;
}

function stubTabs(): EdgeTabStub[] {
	return Array.from({ length: 10 }, (_, index) => ({
		index,
		filled: index < 6,
		color: HOT_CUE_COLORS[index % HOT_CUE_COLORS.length]
	}));
}

export const edgeSamplerUi = $state({
	/** Incremented when a hotkey fires a tab or flips a page so the overlay restarts rAF. */
	animBump: 0,
	lastEdge: 'R' as EdgeId,
	pageByEdge: { L: 0, R: 0 } as Record<EdgeId, number>,
	/** Stays popped when the edge is off or collapsed (playing / armed tabs). */
	held: new Set<string>(),
	tabs: { L: stubTabs(), R: stubTabs() } as Record<EdgeId, EdgeTabStub[]>
});

export function heldKey(edge: EdgeId, index: number): string {
	return `${edge}:${index}`;
}

export function isHeld(edge: EdgeId, index: number): boolean {
	return edgeSamplerUi.held.has(heldKey(edge, index));
}

export function setHeld(edge: EdgeId, index: number, on: boolean): void {
	const key = heldKey(edge, index);
	if (on) edgeSamplerUi.held.add(key);
	else edgeSamplerUi.held.delete(key);
}

export function flipEdgePage(edge: EdgeId, dir: -1 | 1): void {
	edgeSamplerUi.lastEdge = edge;
	edgeSamplerUi.pageByEdge[edge] = Math.max(0, edgeSamplerUi.pageByEdge[edge] + dir);
	edgeSamplerUi.animBump++;
}

export function fireEdgeTab(edge: EdgeId, index: number): void {
	if (index < 0 || index >= 10) return;
	edgeSamplerUi.lastEdge = edge;
	setHeld(edge, index, true);
	edgeSamplerUi.animBump++;
}
