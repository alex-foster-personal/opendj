/** Pure rules for detecting a missed ui-mirror publish interval. */

export const MIRROR_STALL_MS = 5000;

export function classifyMirrorPublishGap(gapMs: number): 'ok' | 'stall' {
	if (!Number.isFinite(gapMs) || gapMs < 0) {
		throw new RangeError(`gapMs must be a non-negative finite number, got ${gapMs}`);
	}
	return gapMs > MIRROR_STALL_MS ? 'stall' : 'ok';
}

export function mirrorStallMessage(gapMs: number): string {
	return `ui-mirror publish missed for ${Math.round(gapMs)}ms`;
}
