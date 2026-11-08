/**
 * Reactive target for `SelectorRing` (K4 / #9396). K3 and K5 are the intended writers.
 *
 * Holds a snapshot of `rect` so callers mutating their own object do not move the ring.
 * A rect can go stale across scroll or resize until the next selection; that stays out of scope.
 */
import type { SelectorRingRect, SelectorRingTarget } from '$lib/keyboard/selector-ring';

function _cloneTarget(target: SelectorRingTarget): SelectorRingTarget {
	const r: SelectorRingRect = { ...target.rect };
	return { id: target.id, kind: target.kind, rect: r };
}

export const selectorRing = $state<{ target: SelectorRingTarget | null }>({ target: null });

export function setSelectorRingTarget(target: SelectorRingTarget): void {
	selectorRing.target = _cloneTarget(target);
}

export function clearSelectorRingTarget(): void {
	selectorRing.target = null;
}
