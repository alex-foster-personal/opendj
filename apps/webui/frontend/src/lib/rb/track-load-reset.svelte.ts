/**
 * Reactive half of the new-track reset config. The plain module owns the
 * saved booleans; this one makes the stem-row button and the panel redraw.
 */

import {
	readTrackLoadReset,
	subscribeTrackLoadReset,
	type TrackLoadResetId,
	type TrackLoadResetPrefs
} from './track-load-reset';

let revision = $state(0);
subscribeTrackLoadReset(() => {
	revision += 1;
});

let open = $state(false);
let focusId = $state<TrackLoadResetId>('stems');
let trigger = $state<HTMLElement | null>(null);

export function trackLoadResetEnabled(id: TrackLoadResetId): boolean {
	revision;
	return readTrackLoadReset()[id];
}

/**
 * The full five-toggle choice with one item changed: the `track_load_reset`
 * command the panel dispatches through the shared performance dispatcher, so
 * the UI and agents take the same path (DECKGAP-7691).
 */
export function trackLoadResetCommand(
	id: TrackLoadResetId,
	on: boolean
): { type: 'track_load_reset' } & TrackLoadResetPrefs {
	if (typeof on !== 'boolean') {
		throw new TypeError(`track load reset ${id} must be a boolean`);
	}
	return { type: 'track_load_reset', ...readTrackLoadReset(), [id]: on };
}

export function trackLoadResetOpen(): boolean {
	return open;
}

export function trackLoadResetFocus(): TrackLoadResetId {
	return focusId;
}

export function trackLoadResetTrigger(): HTMLElement | null {
	return trigger;
}

export function openTrackLoadReset(id: TrackLoadResetId, el: HTMLElement): void {
	focusId = id;
	trigger = el;
	open = true;
}

export function closeTrackLoadReset(): void {
	open = false;
}

/** Second click on the same chip closes the panel. A different chip moves it. */
export function toggleTrackLoadResetPanel(id: TrackLoadResetId, el: HTMLElement): void {
	if (open && trigger === el) {
		open = false;
		return;
	}
	openTrackLoadReset(id, el);
}
