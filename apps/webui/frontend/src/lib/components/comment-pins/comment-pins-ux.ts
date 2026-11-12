/**
 * Shared pin UX helpers for the custom element and the Svelte shell (issue #11295).
 * Copy matches FeedbackPinMarkers / FeedbackPinCard strings; do not invent new labels.
 */
import {
	anchorMovedAtPin,
	pinBoardState,
	type PinBoardBadge,
	type PinBoardState
} from '$lib/rb/feedback-pin-board';
import { pinThread, type ThreadTurn } from '$lib/rb/feedback-pin-thread';
import type { LifecyclePin } from '$lib/rb/feedback';
import type { AnchorishElement } from '$lib/rb/feedback-anchorish';

export function badgeLabel(badge: PinBoardBadge): string {
	switch (badge) {
		case 'anchor-moved':
			return 'anchor moved';
		case 'route-moved':
			return 'route moved';
		case 'harvested':
			return 'harvested';
		case 'regressed':
			return 'regressed';
		default: {
			const _exhaustive: never = badge;
			return _exhaustive;
		}
	}
}

export interface PinExposure extends PinBoardState {
	harvested: boolean;
	anchorMoved: boolean;
}

/** Serializable surface for anchor-moved and harvested states on a pin. */
export function pinExposure(
	pin: LifecyclePin,
	pathname: string,
	anchorMoved: boolean
): PinExposure {
	const board = pinBoardState(pin, pathname, anchorMoved);
	return {
		...board,
		harvested: board.badges.includes('harvested'),
		anchorMoved: board.badges.includes('anchor-moved')
	};
}

export function anchorMovedForPin(
	pin: LifecyclePin,
	elementFromPoint: (x: number, y: number) => AnchorishElement | null
): boolean {
	return anchorMovedAtPin(pin, elementFromPoint);
}

export function threadForPin(pin: Parameters<typeof pinThread>[0]): ThreadTurn[] {
	return pinThread(pin);
}
