/**
 * Pure pointer drag machine for a split handle.
 * Takes pointer coordinates and returns the live px; only endDrag sets commit.
 */
export type DragAxis = 'horizontal' | 'vertical';

export interface PointerPoint {
	x: number;
	y: number;
}

export interface SplitDragOptions {
	axis: DragAxis;
	startPx: number;
	min: number;
	max: number;
}

export interface SplitDragResult {
	px: number;
	commit: boolean;
}

interface ActiveDrag {
	axis: DragAxis;
	origin: number;
	startPx: number;
	min: number;
	max: number;
	px: number;
}

export interface SplitDragMachine {
	beginDrag(pointer: PointerPoint, options: SplitDragOptions): SplitDragResult;
	dragTo(pointer: PointerPoint): SplitDragResult;
	endDrag(): SplitDragResult;
	isDragging(): boolean;
}

function coord(pointer: PointerPoint, axis: DragAxis): number {
	return axis === 'vertical' ? pointer.y : pointer.x;
}

function clamp(value: number, min: number, max: number): number {
	return Math.min(max, Math.max(min, value));
}

export function createSplitDrag(): SplitDragMachine {
	let active: ActiveDrag | null = null;

	function require(call: string): ActiveDrag {
		if (active === null) throw new Error(`${call} called with no drag begun`);
		return active;
	}

	return {
		beginDrag(pointer, options) {
			const px = clamp(options.startPx, options.min, options.max);
			active = {
				axis: options.axis,
				origin: coord(pointer, options.axis),
				startPx: options.startPx,
				min: options.min,
				max: options.max,
				px
			};
			return { px, commit: false };
		},
		dragTo(pointer) {
			const drag = require('dragTo');
			const delta = coord(pointer, drag.axis) - drag.origin;
			drag.px = clamp(drag.startPx + delta, drag.min, drag.max);
			return { px: drag.px, commit: false };
		},
		endDrag() {
			const drag = require('endDrag');
			active = null;
			return { px: drag.px, commit: true };
		},
		isDragging() {
			return active !== null;
		}
	};
}
