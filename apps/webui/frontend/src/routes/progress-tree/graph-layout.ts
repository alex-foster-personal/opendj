/**
 * Dependency-graph geometry for the /progress-tree Graph tab.
 *
 * Pure, dependency-free (no d3): computes wave columns and a flat set of
 * positioned node chips + edges the SVG renders directly. Everything here is
 * derived live from the ledger - wave counts are never hardcoded, so if the
 * data drifts from the conventions doc the rendered truth follows the data.
 *
 * Wave rule (mirrors .planning/FANOUT-CONVENTIONS.md):
 *   wave 0 = an outstanding node with no unmet deps. A dep on a built/
 *   verified/merged/user-finalized node counts as met; a dep on another
 *   outstanding node is unmet until that dep is itself placed. wave(n) = 1 +
 *   max(wave of outstanding deps). Nodes tangled in a cycle are placed one
 *   past the deepest placed wave so they still render.
 */

import { isDoneStatus, isOutstanding, parseLanes, type ProgressNode } from './types';

//----- wave scheduling -----------------------------------------------------

export interface WaveResult {
	/** node id -> wave index, only for outstanding nodes. */
	waveById: Map<string, number>;
	/** wave index -> count of outstanding nodes in it (live truth). */
	countByWave: Map<number, number>;
	maxWave: number;
}

export function computeWaves(nodes: ProgressNode[]): WaveResult {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const outstanding = nodes.filter(isOutstanding).map((n) => n.id);
	const outstandingSet = new Set(outstanding);
	const waveById = new Map<string, number>();

	// Fixpoint: place a node once every outstanding dep it has is placed.
	let changed = true;
	while (changed) {
		changed = false;
		for (const id of outstanding) {
			if (waveById.has(id)) continue;
			const node = byId.get(id);
			if (node === undefined) continue;
			let ready = true;
			let deepest = -1;
			for (const dep of node.deps) {
				const depNode = byId.get(dep);
				if (depNode === undefined) continue; // unknown dep: treat as met
				if (isDoneStatus(depNode.status)) continue; // done dep is met
				if (!outstandingSet.has(dep)) continue; // non-outstanding, non-done: met
				const depWave = waveById.get(dep);
				if (depWave === undefined) {
					ready = false;
					break;
				}
				deepest = Math.max(deepest, depWave);
			}
			if (ready) {
				waveById.set(id, deepest + 1);
				changed = true;
			}
		}
	}

	// Cycle fallback: anything still unplaced goes one past the deepest wave.
	let maxWave = -1;
	for (const w of waveById.values()) maxWave = Math.max(maxWave, w);
	const fallbackWave = maxWave + 1;
	for (const id of outstanding) {
		if (!waveById.has(id)) waveById.set(id, fallbackWave);
	}
	maxWave = -1;
	for (const w of waveById.values()) maxWave = Math.max(maxWave, w);

	const countByWave = new Map<number, number>();
	for (const w of waveById.values()) countByWave.set(w, (countByWave.get(w) ?? 0) + 1);

	return { waveById, countByWave, maxWave };
}

//----- graph layout --------------------------------------------------------

export interface GraphNodeBox {
	node: ProgressNode;
	lanes: string[];
	done: boolean;
	col: number;
	x: number;
	y: number;
	w: number;
	h: number;
	cx: number;
	cy: number;
}

export interface GraphEdge {
	id: string;
	from: string;
	to: string;
}

export interface GraphColumn {
	key: string;
	label: string;
	x: number;
}

export interface GraphLayout {
	boxes: GraphNodeBox[];
	edges: GraphEdge[];
	columns: GraphColumn[];
	boxById: Map<string, GraphNodeBox>;
	width: number;
	height: number;
	allLanes: string[];
	waves: WaveResult;
}

// Wave columns run 2x wider than the original 168 so a chip has room for a
// longer title plus the build-state icon (CHANGE 3, Wed 22 Jul 2026).
const CHIP_W = 336;
const CHIP_H = 48;
const COL_GAP = 56;
const ROW_GAP = 16;
const PAD_X = 24;
const HEADER_H = 40;
const PAD_BOTTOM = 24;
const COL_W = CHIP_W + COL_GAP;

/**
 * Build the graph geometry for the given ledger nodes.
 * `showAll` false = outstanding nodes plus any done nodes an outstanding node
 * depends on (rendered dimmed). `showAll` true = every node.
 */
export function buildGraphLayout(nodes: ProgressNode[], showAll: boolean): GraphLayout {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const waves = computeWaves(nodes);

	// Which nodes are on screen.
	const displayed = new Set<string>();
	for (const node of nodes) {
		if (showAll || isOutstanding(node)) displayed.add(node.id);
	}
	if (!showAll) {
		// Pull in done nodes that outstanding nodes depend on (met deps stay visible).
		for (const node of nodes) {
			if (!isOutstanding(node)) continue;
			for (const dep of node.deps) {
				const depNode = byId.get(dep);
				if (depNode !== undefined && isDoneStatus(depNode.status)) displayed.add(dep);
			}
		}
	}

	const displayedNodes = nodes.filter((n) => displayed.has(n.id));
	const doneShown = displayedNodes.some((n) => !isOutstanding(n));
	const doneOffset = doneShown ? 1 : 0;

	function columnFor(node: ProgressNode): number {
		if (!isOutstanding(node)) return 0; // done column (leftmost)
		return doneOffset + (waves.waveById.get(node.id) ?? 0);
	}

	// Group by column, preserving ledger order within a column.
	const byColumn = new Map<number, ProgressNode[]>();
	for (const node of displayedNodes) {
		const col = columnFor(node);
		const list = byColumn.get(col) ?? [];
		list.push(node);
		byColumn.set(col, list);
	}

	const columnCount = doneOffset + waves.maxWave + 1;
	const boxes: GraphNodeBox[] = [];
	const boxById = new Map<string, GraphNodeBox>();
	let maxRows = 0;

	for (let col = 0; col < columnCount; col += 1) {
		const list = byColumn.get(col) ?? [];
		maxRows = Math.max(maxRows, list.length);
		const x = PAD_X + col * COL_W;
		for (let row = 0; row < list.length; row += 1) {
			const node = list[row];
			const y = HEADER_H + row * (CHIP_H + ROW_GAP);
			const box: GraphNodeBox = {
				node,
				lanes: parseLanes(node.notes),
				done: !isOutstanding(node),
				col,
				x,
				y,
				w: CHIP_W,
				h: CHIP_H,
				cx: x + CHIP_W / 2,
				cy: y + CHIP_H / 2
			};
			boxes.push(box);
			boxById.set(node.id, box);
		}
	}

	// Column headers.
	const columns: GraphColumn[] = [];
	for (let col = 0; col < columnCount; col += 1) {
		const x = PAD_X + col * COL_W;
		if (doneShown && col === 0) {
			columns.push({ key: 'done', label: 'Done deps', x });
			continue;
		}
		const wave = col - doneOffset;
		const count = waves.countByWave.get(wave) ?? 0;
		columns.push({ key: `wave-${wave}`, label: `Wave ${wave} (n=${count})`, x });
	}

	// Edges: dep -> dependent, only where both endpoints are on screen.
	const edges: GraphEdge[] = [];
	for (const node of displayedNodes) {
		for (const dep of node.deps) {
			if (!displayed.has(dep)) continue;
			edges.push({ id: `${dep}->${node.id}`, from: dep, to: node.id });
		}
	}

	const allLanes = Array.from(new Set(boxes.flatMap((b) => b.lanes))).sort();
	const width = Math.max(COL_W, PAD_X * 2 + columnCount * COL_W - COL_GAP);
	const height = HEADER_H + Math.max(1, maxRows) * (CHIP_H + ROW_GAP) + PAD_BOTTOM;

	return { boxes, edges, columns, boxById, width, height, allLanes, waves };
}

/** Smooth cubic bezier from a dep box's right edge to a dependent's left edge. */
export function edgePath(from: GraphNodeBox, to: GraphNodeBox): string {
	const x1 = from.x + from.w;
	const y1 = from.cy;
	const x2 = to.x;
	const y2 = to.cy;
	const dx = Math.max(28, (x2 - x1) / 2);
	return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}
