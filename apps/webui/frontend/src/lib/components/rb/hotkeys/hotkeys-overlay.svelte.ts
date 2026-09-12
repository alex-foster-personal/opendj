/**
 * Global hotkeys overlay open/close + view/query.
 * Mounted from root layout so "/" and "?" work on /performance and the shell.
 */
import {
	createHotkeysOverlayMachine,
	isHotkeysOverlayVisible,
	type HotkeysOverlayGestureState
} from './hotkeys-gesture';
import type { HotkeysOverlayView } from './hotkeys-filter';

const machine = createHotkeysOverlayMachine();

let open = $state(false);
let view = $state<HotkeysOverlayView>('grid');
let query = $state('');

function _pull(): void {
	const next: HotkeysOverlayGestureState = machine.getState();
	open = isHotkeysOverlayVisible(next);
	view = next.view;
	query = next.query;
}

export function isHotkeysOverlayOpen(): boolean {
	return open;
}

export function getHotkeysOverlayQuery(): string {
	return query;
}

export function getHotkeysOverlayView(): HotkeysOverlayView {
	return view;
}

export function beginHotkeysOverlayHold(): void {
	machine.slashKeyDown();
	_pull();
}

export function endHotkeysOverlayHold(): void {
	machine.slashKeyUp();
	_pull();
}

export function resolveHotkeysOverlayHoldOnBlur(): void {
	machine.resolveBlur();
	_pull();
}

export function showHotkeysOverlay(): void {
	machine.show();
	_pull();
}

export function hideHotkeysOverlay(): void {
	machine.hide();
	_pull();
}

export function toggleHotkeysOverlay(): void {
	machine.toggle();
	_pull();
}

export function setHotkeysOverlayView(next: HotkeysOverlayView): void {
	machine.setView(next);
	_pull();
}

export function setHotkeysOverlayQuery(next: string): void {
	machine.setQuery(next);
	_pull();
}

/** Reactive snapshot for Svelte components (read via $derived in overlay). */
export const hotkeysOverlay = {
	get open() {
		return open;
	},
	get view() {
		return view;
	},
	get query() {
		return query;
	}
};
