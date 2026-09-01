<script lang="ts">
	// Pitch fader + range switcher (pitch-slider, ledger issue 186): vertical
	// centered-zero fader drives the real engine tempo ratio (setTempoRatio),
	// 8/16/WIDE buttons drive setPitchRange. JogDial renders the resulting
	// pitch%/range readout; this component is the only writer of both.
	import { PITCH_RANGES } from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { faderValueFromPitchRatio, pitchRatioFromFaderValue } from './pitch-fader-geometry';

	let {
		deck,
		pitchRange,
		pending,
		onTempoChange,
		onRangeChange
	}: {
		deck: DeckState;
		pitchRange: PitchRange;
		pending: boolean;
		onTempoChange: (ratio: number) => Promise<void>;
		onRangeChange: (range: PitchRange) => Promise<void>;
	} = $props();

	const TRACK_H = 96; // matches .rb-fader height in theme.css
	const THUMB_H = 12; // matches .rb-fader-thumb height in theme.css
	const KEY_STEP_PCT = 0.1; // ArrowUp/Down nudges pitch by 0.1%

	let activePointerId: number | null = null;

	// 0 = -range%, 0.5 = 0% (ratio 1.0), 1 = +range% (top = faster).
	const value: number = $derived(faderValueFromPitchRatio(deck.pitch, pitchRange));
	const thumbTopPx: number = $derived((1 - value) * (TRACK_H - THUMB_H));

	function _valueFromEvent(e: PointerEvent): number {
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		const y = e.clientY - rect.top - THUMB_H / 2;
		return Math.min(1, Math.max(0, 1 - y / (TRACK_H - THUMB_H)));
	}

	function _setTempoFromValue(value: number): void {
		// runPerformanceCommandFromUi owns errors and route-session generation.
		// Do not await pointer events: a drag must keep sampling while the prior
		// scheduled tempo update is pending on the deck/sync command scope.
		void onTempoChange(pitchRatioFromFaderValue(value, pitchRange));
	}

	function _setTempoFromKey(value: number): void {
		_setTempoFromValue(faderValueFromPitchRatio(value, pitchRange));
	}

	function handlePointerDown(e: PointerEvent): void {
		if (pending) return;
		const target = e.currentTarget as HTMLElement;
		target.focus();
		activePointerId = e.pointerId;
		target.setPointerCapture(e.pointerId);
		_setTempoFromValue(_valueFromEvent(e));
	}

	function handlePointerMove(e: PointerEvent): void {
		if (activePointerId !== e.pointerId) return;
		_setTempoFromValue(_valueFromEvent(e));
	}

	function handlePointerDone(e: PointerEvent): void {
		if (activePointerId !== e.pointerId) return;
		activePointerId = null;
		const target = e.currentTarget as HTMLElement;
		if (target.hasPointerCapture(e.pointerId)) target.releasePointerCapture(e.pointerId);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (pending) return;
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			_setTempoFromKey(deck.pitch + KEY_STEP_PCT / 100);
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			_setTempoFromKey(deck.pitch - KEY_STEP_PCT / 100);
		} else if (e.key === 'PageUp') {
			e.preventDefault();
			_setTempoFromKey(deck.pitch + 0.01);
		} else if (e.key === 'PageDown') {
			e.preventDefault();
			_setTempoFromKey(deck.pitch - 0.01);
		} else if (e.key === 'Home') {
			e.preventDefault();
			_setTempoFromValue(0);
		} else if (e.key === 'End') {
			e.preventDefault();
			_setTempoFromValue(1);
		}
	}
</script>

<div class="pitch-fader">
	<div
		class="rb-fader"
		role="slider"
		aria-label={`deck ${deck.deck_id} pitch fader`}
		aria-orientation="vertical"
		aria-valuemin={-pitchRange}
		aria-valuemax={pitchRange}
		aria-valuenow={Number(((deck.pitch - 1) * 100).toFixed(2))}
		aria-disabled={pending}
		tabindex="0"
		data-performance-control="pitch"
		use:wheelAdjust={{
			step: WHEEL_STEP.pitch,
			get: () => value,
			set: _setTempoFromValue,
			disabled: deck.stable_id === null
		}}
		onpointerdown={handlePointerDown}
		onpointermove={handlePointerMove}
		onpointerup={handlePointerDone}
		onpointercancel={handlePointerDone}
		onkeydown={handleKeyDown}
	>
		<div class="rb-fader-track"></div>
		<div class="center-tick"></div>
		<div class="rb-fader-thumb" style={`top: ${thumbTopPx}px;`}></div>
	</div>

	<div class="range-buttons">
		{#each PITCH_RANGES as range (range)}
			<button
				class="rb-lit-button small"
				class:lit={pitchRange === range}
				disabled={pending}
				aria-pressed={pitchRange === range}
				data-performance-control="pitch-range"
				data-range={range}
				data-state={pitchRange === range ? 'on' : 'off'}
				title={`pitch range ${range === 100 ? 'WIDE' : `+-${range}%`}`}
				onclick={async () => await onRangeChange(range)}
			>
				{range === 100 ? 'W' : range}
			</button>
		{/each}
	</div>
</div>

<style>
	.pitch-fader {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		flex: 0 0 auto;
	}
	.rb-fader {
		position: relative;
		cursor: ns-resize;
		touch-action: none;
		outline: none;
	}
	.rb-fader:focus-visible .rb-fader-thumb {
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}
	.center-tick {
		position: absolute;
		left: -3px;
		right: -3px;
		top: 50%;
		height: 1px;
		margin-top: -0.5px;
		background: var(--rb-text-dim);
		pointer-events: none;
	}
	.range-buttons {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.range-buttons .small {
		font-size: 8px;
		padding: 1px 5px;
		min-width: 18px;
		text-align: center;
	}
</style>
