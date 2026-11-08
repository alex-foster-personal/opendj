/**
 * Pure helpers for the keyboard selector focus ring (#9396, K4). No runtime imports.
 *
 * Shapes match K1's `SelectorRect`, `SelectorNodeKind` and `SelectorNode` (issue #9393,
 * PR #9550): a K1 node adds `parentId` and can be assigned to `SelectorRingTarget` where
 * only `id`, `rect` and `kind` are read.
 *
 * Rectangles are viewport-relative (`getBoundingClientRect`). The ring is mounted under
 * `.perf-root` (`position: fixed; inset: 0`), so those coordinates align 1:1 with the
 * overlay.
 */
export const SELECTOR_RING_INSET_PX = 2;

export type SelectorRingRect = { x: number; y: number; width: number; height: number };
export type SelectorRingKind = 'section' | 'group' | 'leaf';
export type SelectorRingTarget = { id: string; rect: SelectorRingRect; kind: SelectorRingKind };

function _assertFinite(value: number, label: string): void {
	if (!Number.isFinite(value)) throw new RangeError(`${label} is not finite`);
}

/**
 * Outset box: node rect plus `inset` pixels on each side (default 2, VS Code focusBorder-style).
 */
export function insetSelectorRingRect(
	rect: SelectorRingRect,
	inset: number = SELECTOR_RING_INSET_PX
): SelectorRingRect {
	_assertFinite(rect.x, 'rect.x');
	_assertFinite(rect.y, 'rect.y');
	_assertFinite(rect.width, 'rect.width');
	_assertFinite(rect.height, 'rect.height');
	if (rect.width < 0 || rect.height < 0) {
		throw new RangeError('rect width and height must be non-negative');
	}
	return {
		x: rect.x - inset,
		y: rect.y - inset,
		width: rect.width + 2 * inset,
		height: rect.height + 2 * inset
	};
}

/** Position the ring using transform only (no layout reads in the animation path). */
export function selectorRingTransform(rect: SelectorRingRect): string {
	return `translate3d(${rect.x}px, ${rect.y}px, 0)`;
}

export function selectorRingBorderStyle(kind: SelectorRingKind): 'dashed' | 'solid' {
	return kind === 'section' ? 'dashed' : 'solid';
}
