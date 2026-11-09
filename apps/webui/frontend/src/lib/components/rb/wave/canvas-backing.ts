/**
 * WaveRow canvas backing-store sizing (PERF-PAINT-10546, issue #10219).
 * While the row is being resized the backing store grows in 128 CSS px
 * buckets so a height drag reallocates the bitmap only when a bucket
 * boundary is crossed; once the size has been still for RESIZE_SETTLE_MS
 * the exact size is restored once.
 */
export const RESIZE_SETTLE_MS = 250;
export const BACKING_BUCKET_CSS_PX = 128;

export type BackingRequest = { cssW: number; cssH: number; dpr: number; resizing: boolean };
export type BackingSize = {
	/** CSS px the canvas element is styled to (bucket size while resizing). */
	cssW: number;
	cssH: number;
	/** Device px for canvas.width / canvas.height. */
	width: number;
	height: number;
	/** True when the CSS size exceeds the painted region and the wrap must clip it. */
	bucketed: boolean;
};

function _requirePositiveInt(name: string, v: number): void {
	if (!Number.isInteger(v) || v <= 0)
		throw new Error(`backingSize: ${name} must be a positive integer, got ${v}`);
}

function _bucketUp(v: number): number {
	return Math.ceil(v / BACKING_BUCKET_CSS_PX) * BACKING_BUCKET_CSS_PX;
}

export function backingSize(req: BackingRequest): BackingSize {
	_requirePositiveInt('cssW', req.cssW);
	_requirePositiveInt('cssH', req.cssH);
	if (!Number.isFinite(req.dpr) || req.dpr <= 0)
		throw new Error(`backingSize: dpr must be finite positive, got ${req.dpr}`);
	const cssW = req.resizing ? _bucketUp(req.cssW) : req.cssW;
	const cssH = req.resizing ? _bucketUp(req.cssH) : req.cssH;
	return {
		cssW,
		cssH,
		width: Math.round(cssW * req.dpr),
		height: Math.round(cssH * req.dpr),
		bucketed: cssW !== req.cssW || cssH !== req.cssH
	};
}

export function isResizing(lastResizeAtMs: number | null, nowMs: number): boolean {
	if (lastResizeAtMs === null) return false;
	return nowMs - lastResizeAtMs < RESIZE_SETTLE_MS;
}
