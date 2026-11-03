/**
 * Pure geometry for the AutoPlay hover curve (in-table arc diagram).
 * Row Y = index * rowHeight - scrollTop (same formula as TrackTable master fold).
 * No DOM measurement - works for virtualized off-window rows.
 */

export interface CurvePoint {
	stable_id: string;
	rank: number;
	y: number;
}

export interface CurveSegment {
	from: CurvePoint;
	to: CurvePoint;
	/** Signed px bulge for the quadratic control point. */
	bulge: number;
	/** True when this hop skips one or more ranks currently off-window. */
	skips: boolean;
}

const LANE_STEP_PX = 12;
const MAX_BULGE_PX = 40;

/** First hop on the rainbow. Later hops keep this hue until they fold. */
export const CURVE_HUE_START = 12;
/**
 * Rainbow step when a hop goes backward or crosses another hop.
 * Small enough that the new color still reads as a neighbor of the last one.
 */
export const CURVE_HUE_STEP = 14;

export interface ChainHopPaint {
	fromId: string;
	toId: string;
	/** +1 next row is below, -1 next row is above. */
	direction: 1 | -1;
	hue: number;
}

function _rangesCross(
	a: { minY: number; maxY: number },
	b: { minY: number; maxY: number }
): boolean {
	// Shared endpoints touch but do not cross. Only an overlapping interior counts.
	return Math.min(a.maxY, b.maxY) - Math.max(a.minY, b.minY) > 0;
}

/**
 * Color for each consecutive pair in chart order. Hue stays put on a forward
 * hop that does not cross another hop, and steps along the rainbow otherwise.
 */
export function paintChainHops(
	chain: readonly string[],
	rowIndexOf: ReadonlyMap<string, number>
): ChainHopPaint[] {
	const points: { id: string; index: number }[] = [];
	for (const id of chain) {
		const index = rowIndexOf.get(id);
		if (index === undefined) continue;
		points.push({ id, index });
	}
	if (points.length < 2) return [];

	const spans = points.slice(0, -1).map((from, i) => {
		const to = points[i + 1];
		return { minY: Math.min(from.index, to.index), maxY: Math.max(from.index, to.index) };
	});

	let hue = CURVE_HUE_START;
	const out: ChainHopPaint[] = [];
	for (let i = 0; i < points.length - 1; i++) {
		const from = points[i];
		const to = points[i + 1];
		const backwards = to.index < from.index;
		let crosses = false;
		for (let j = 0; j < spans.length; j++) {
			if (j !== i && _rangesCross(spans[i], spans[j])) {
				crosses = true;
				break;
			}
		}
		if (i > 0 && (backwards || crosses)) hue = (hue + CURVE_HUE_STEP) % 360;
		out.push({
			fromId: from.id,
			toId: to.id,
			direction: backwards ? -1 : 1,
			hue
		});
	}
	return out;
}

/** Stroke/fill for a hop hue. Light enough to read on the dark performance ground. */
export function hopStroke(hue: number): string {
	return `hsl(${hue} 72% 58%)`;
}

export interface ArrowHead {
	/** Empty when the hop is too short to show a head. */
	points: string;
	baseY: number;
}

/** Triangle at the destination, pointing up or down the list, clear of the end node. */
export function arrowHead(fromY: number, toY: number, x: number): ArrowHead {
	const dy = toY - fromY;
	const dir = dy >= 0 ? 1 : -1;
	const span = Math.abs(dy);
	if (span < 8) return { points: '', baseY: toY };
	const tipGap = Math.min(6, span * 0.22);
	const length = Math.min(11, Math.max(7, span * 0.38));
	const half = 5;
	const tipY = toY - dir * tipGap;
	let baseY = tipY - dir * length;
	if (dir > 0) baseY = Math.min(tipY - 4, Math.max(fromY, baseY));
	else baseY = Math.max(tipY + 4, Math.min(fromY, baseY));
	const r = (n: number) => Math.round(n * 10) / 10;
	return {
		points: `${r(x)},${r(tipY)} ${r(x - half)},${r(baseY)} ${r(x + half)},${r(baseY)}`,
		baseY
	};
}

/** Greedy interval-graph lane assignment. */
export function assignLanes(spans: ReadonlyArray<{ minY: number; maxY: number }>): number[] {
	const laneMaxY: number[] = [];
	return spans.map((span) => {
		let lane = laneMaxY.findIndex((maxY) => maxY < span.minY);
		if (lane === -1) lane = laneMaxY.length;
		laneMaxY[lane] = span.maxY;
		return lane;
	});
}

function _laneToBulge(lane: number): number {
	const side = lane % 2 === 0 ? 1 : -1;
	const step = Math.floor(lane / 2) + 1;
	return side * Math.min(MAX_BULGE_PX, step * LANE_STEP_PX);
}

/** Ordered curve segments for chain members inside the padded viewport. */
export function buildCurveSegments(params: {
	chain: readonly string[];
	rankOf: ReadonlyMap<string, number>;
	rowIndexOf: ReadonlyMap<string, number>;
	rowHeight: number;
	scrollTop: number;
	viewportHeight: number;
	pad: number;
}): CurveSegment[] {
	const { chain, rankOf, rowIndexOf, rowHeight, scrollTop, viewportHeight, pad } = params;
	const minY = -pad;
	const maxY = viewportHeight + pad;
	const points: CurvePoint[] = [];
	for (const stable_id of chain) {
		const rank = rankOf.get(stable_id);
		const index = rowIndexOf.get(stable_id);
		if (rank === undefined || index === undefined) continue;
		const y = index * rowHeight - scrollTop + rowHeight / 2;
		if (y < minY || y > maxY) continue;
		points.push({ stable_id, rank, y });
	}
	points.sort((a, b) => a.rank - b.rank);
	if (points.length < 2) return [];

	const spans = [];
	for (let i = 0; i < points.length - 1; i++) {
		const from = points[i];
		const to = points[i + 1];
		spans.push({ minY: Math.min(from.y, to.y), maxY: Math.max(from.y, to.y) });
	}
	const lanes = assignLanes(spans);
	const out: CurveSegment[] = [];
	for (let i = 0; i < points.length - 1; i++) {
		const from = points[i];
		const to = points[i + 1];
		out.push({
			from,
			to,
			bulge: _laneToBulge(lanes[i] ?? 0),
			skips: to.rank - from.rank > 1
		});
	}
	return out;
}

/** SVG quadratic path for one segment (x is column center). `endY` stops the stroke at an arrow base. */
export function segmentPath(seg: CurveSegment, x: number, endY?: number): string {
	const y2 = endY ?? seg.to.y;
	const midY = (seg.from.y + seg.to.y) / 2;
	const cx = x + seg.bulge;
	return `M ${x} ${seg.from.y} Q ${cx} ${midY} ${x} ${y2}`;
}
