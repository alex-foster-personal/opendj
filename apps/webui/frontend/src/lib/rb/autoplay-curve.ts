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

/** SVG quadratic path for one segment (x is column center). */
export function segmentPath(seg: CurveSegment, x: number): string {
	const midY = (seg.from.y + seg.to.y) / 2;
	const cx = x + seg.bulge;
	return `M ${x} ${seg.from.y} Q ${cx} ${midY} ${x} ${seg.to.y}`;
}
