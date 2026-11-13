/**
 * LIBM-96 (issue #13731): edge auto-scroll while a drag hovers a scroll
 * container. HTML5 drag-and-drop delivers neither wheel nor mouse events, so a
 * playlist scrolled out of view could never be reached by a dragged track.
 *
 * Hygiene mimicked from Chromium's built-in drag autoscroll: a 40 px band at
 * the top and bottom edge, speed proportional to depth into the band, capped
 * at 20 px per animation frame.
 */

/** Edge band depth and the per-frame speed cap, in CSS pixels. */
export interface EdgeAutoscrollConfig {
	bandPx: number;
	maxStepPx: number;
}

/** Chromium's autoscroll band and cap (mimicked, LIBM-96). */
export const EDGE_AUTOSCROLL: EdgeAutoscrollConfig = { bandPx: 40, maxStepPx: 20 };

/** The container's viewport edges plus its scroll position, one snapshot. */
export interface ScrollRect {
	top: number;
	bottom: number;
	scrollTop: number;
	scrollHeight: number;
	clientHeight: number;
}

/** Pixels to scroll this frame for a pointer at `pointerY`: negative up, positive down, 0 outside the bands. */
export function edgeAutoscrollDelta(
	pointerY: number,
	rect: ScrollRect,
	cfg: EdgeAutoscrollConfig = EDGE_AUTOSCROLL
): number {
	const fromTop = pointerY - rect.top;
	const fromBottom = rect.bottom - pointerY;
	const maxScrollTop = Math.max(0, rect.scrollHeight - rect.clientHeight);
	if (fromTop < cfg.bandPx && fromTop <= fromBottom) {
		const up = Math.min(_bandStep(fromTop, cfg), rect.scrollTop);
		return up > 0 ? -up : 0;
	} else if (fromBottom < cfg.bandPx) {
		const step = _bandStep(fromBottom, cfg);
		return Math.min(step, Math.max(0, maxScrollTop - rect.scrollTop));
	}
	return 0;
}

function _bandStep(distanceFromEdge: number, cfg: EdgeAutoscrollConfig): number {
	const depth = Math.min(cfg.bandPx, cfg.bandPx - distanceFromEdge);
	return (cfg.maxStepPx * depth) / cfg.bandPx;
}

// ------------------------------------------------------------------ driver

/** The element surface the driver reads and scrolls. */
export interface AutoscrollTarget {
	scrollTop: number;
	readonly scrollHeight: number;
	readonly clientHeight: number;
	getBoundingClientRect(): { top: number; bottom: number };
}

/**
 * The frame clock the loop runs on. The calling component passes
 * requestAnimationFrame: this module is not a render module, so it never
 * names the frame clock itself (AUDIOLIVE-11). The loop is paint-only and
 * pointer-driven, so a hidden tab pausing it loses nothing.
 */
export interface AutoscrollFrames {
	request: (callback: () => void) => number;
	cancel: (handle: number) => void;
}

export interface EdgeAutoscroll {
	/** Record the latest drag pointer Y and run frames while it sits in a band. */
	update(pointerY: number): void;
	/** Stop scrolling; no further animation frame is requested. */
	stop(): void;
	readonly running: boolean;
}

/** A frame loop on `frames` that scrolls `target()` toward the edge band the pointer is in. */
export function createEdgeAutoscroll(
	target: () => AutoscrollTarget | null,
	frames: AutoscrollFrames,
	cfg: EdgeAutoscrollConfig = EDGE_AUTOSCROLL
): EdgeAutoscroll {
	let pointerY = 0;
	let handle: number | null = null;

	function _deltaNow(): number {
		const el = target();
		if (el === null) return 0;
		const box = el.getBoundingClientRect();
		return edgeAutoscrollDelta(
			pointerY,
			{
				top: box.top,
				bottom: box.bottom,
				scrollTop: el.scrollTop,
				scrollHeight: el.scrollHeight,
				clientHeight: el.clientHeight
			},
			cfg
		);
	}

	function _frame(): void {
		handle = null;
		const delta = _deltaNow();
		const el = target();
		if (delta === 0 || el === null) return;
		el.scrollTop += delta;
		handle = frames.request(_frame);
	}

	return {
		update(y: number): void {
			pointerY = y;
			if (handle === null && _deltaNow() !== 0) handle = frames.request(_frame);
		},
		stop(): void {
			if (handle !== null) frames.cancel(handle);
			handle = null;
		},
		get running(): boolean {
			return handle !== null;
		}
	};
}
