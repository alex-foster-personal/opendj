/**
 * Performance-route listeners for mixer selection and horizontal wheel (MIXUX-08).
 */
import { mixerState } from './audio-engine.svelte';
import {
	detectWheelInputKind,
	horizontalWheelDirection,
	scaledWheelStep,
	WHEEL_STEP
} from './wheel-adjust';
import { shouldRouteHorizontalWheel, knobCommandType } from './mixer-horizontal-wheel';
import { getSelectedDecks, resetToSingle } from './mixer-selection.svelte';
import { uiPrefs } from './prefs.svelte';
import { runPerformanceCommandFromUi } from './performance-ipc.svelte';
import { tickFaderGhostIdle } from './fader-ghost.svelte';

function _clamp01(value: number): number {
	return Math.min(1, Math.max(0, value));
}

export function installMixerPerformanceListeners(): () => void {
	function onKeyDown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		const selected = getSelectedDecks();
		if (selected.length <= 1) return;
		resetToSingle(selected[0] ?? null);
	}

	function onPointerDown(event: PointerEvent): void {
		if (event.shiftKey) return;
		if (event.target instanceof Element && event.target.closest('[data-library-root]') !== null) {
			return;
		}
		if (getSelectedDecks().length <= 1) return;
		const deckTarget =
			event.target instanceof Element
				? event.target.closest('[data-deck-hover], [data-mixer-channel]')
				: null;
		if (deckTarget !== null) return;
		resetToSingle(getSelectedDecks()[0] ?? null);
	}

	function onWheel(event: WheelEvent): void {
		if (!shouldRouteHorizontalWheel(event, event.target)) return;
		const direction = horizontalWheelDirection(event);
		if (direction === 0) return;
		const decks = getSelectedDecks();
		if (decks.length === 0) return;
		event.preventDefault();
		const step = scaledWheelStep(WHEEL_STEP.knob, detectWheelInputKind(event));
		const knob = knobCommandType(uiPrefs.horizontal_wheel_knob);
		for (const deck of decks) {
			const current = mixerState.channels[deck][knob];
			const next = _clamp01(current + direction * step);
			void runPerformanceCommandFromUi({ type: knob, deck, value: next });
		}
	}

	let ghostIdleRaf = 0;
	function ghostIdleLoop(): void {
		tickFaderGhostIdle(performance.now());
		ghostIdleRaf = requestAnimationFrame(ghostIdleLoop);
	}
	ghostIdleRaf = requestAnimationFrame(ghostIdleLoop);

	window.addEventListener('keydown', onKeyDown, true);
	window.addEventListener('pointerdown', onPointerDown, true);
	window.addEventListener('wheel', onWheel, { passive: false, capture: true });

	return () => {
		cancelAnimationFrame(ghostIdleRaf);
		window.removeEventListener('keydown', onKeyDown, true);
		window.removeEventListener('pointerdown', onPointerDown, true);
		window.removeEventListener('wheel', onWheel, true);
	};
}
