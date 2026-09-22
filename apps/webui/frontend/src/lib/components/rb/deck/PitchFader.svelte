<script lang="ts">
	// Pitch fader + range switcher (pitch-slider, ledger issue 186): vertical
	// centered-zero fader drives the real engine tempo ratio (setTempoRatio),
	// 8/16/WIDE buttons drive setPitchRange. JogDial renders the resulting
	// pitch%/range readout; this component is the only writer of both.
	import { PITCH_RANGES } from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { coalesceLatest } from '$lib/rb/coalesce';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-state.svelte';
	import { faderValueFromPitchRatio, pitchRatioFromFaderValue } from './pitch-fader-geometry';
	import { thumbOffsetPx, valueFromPointer } from '$lib/rb/pitch-fader-geometry';

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

	const KEY_STEP_PCT = 0.1; // ArrowUp/Down nudges pitch by 0.1%

	/** The live track height. `.rb-fader` is `height: 100%`, so this cannot be
	 * a constant - it was one, and 0% drew above centre on every fader taller
	 * than the assumed 96px (pin ebb1def0234c). Measured from the element the
	 * pointer maths already measures, so the two agree by construction. */
	let trackEl: HTMLDivElement | undefined = $state();
	let trackH = $state(96);

	let activePointerId: number | null = null;
	const tempoDispatcher = coalesceLatest(async (ratio: number) => await onTempoChange(ratio));

	$effect(() => {
		return () => tempoDispatcher.cancel();
	});

	// 0 = -range%, 0.5 = 0% (ratio 1.0), 1 = +range% (top = faster).
	const value: number = $derived(faderValueFromPitchRatio(deck.pitch, pitchRange));
	const thumbTopPx: number = $derived(thumbOffsetPx(value, trackH));
	const takeoverGhost = $derived(midiTakeoverGhost(`deck:${deck.deck_id}:pitch`));
	const ghostTopPx = $derived(takeoverGhost === null ? 0 : thumbOffsetPx(takeoverGhost.value, trackH));

	$effect(() => {
		const el = trackEl;
		if (!el) return;
		const measure = (): void => {
			trackH = el.getBoundingClientRect().height;
		};
		measure();
		const ro = new ResizeObserver(measure);
		ro.observe(el);
		return () => ro.disconnect();
	});

	function _valueFromEvent(e: PointerEvent): number {
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		return valueFromPointer(e.clientY - rect.top, rect.height);
	}

	function _setTempoFromValue(value: number): void {
		// Do not await pointer events. A continuous drag sends its first value
		// promptly, then holds only its latest value while the deck/sync scope
		// drains, so a slow schedule cannot replay stale fader positions.
		void tempoDispatcher.request(pitchRatioFromFaderValue(value, pitchRange));
	}

	function _setTempoFromKey(value: number): void {
		_setTempoFromValue(faderValueFromPitchRatio(value, pitchRange));
	}

	function handlePointerDown(e: PointerEvent): void {
		if (pending || deck.stable_id === null) return;
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

	function handleDoubleClick(): void {
		if (deck.stable_id === null) return;
		// No `pending` guard, unlike handlePointerDown/handleKeyDown: those gate
		// STARTING a new gesture while a command is in flight (see
		// handlePointerMove below, which has no such guard either once a
		// gesture is already granted). A double-click is not a continuation -
		// it is a discrete reset the user just performed, and its two
		// constituent pointerdowns already dispatched their own tempo commands
		// before this handler runs. Gating the reset on `pending` let those
		// still-in-flight commands win and silently drop the reset exactly
		// when the engine is busiest - the reset must supersede them instead.
		_setTempoFromValue(0.5);
	}

	function handlePointerDone(e: PointerEvent): void {
		if (activePointerId !== e.pointerId) return;
		activePointerId = null;
		const target = e.currentTarget as HTMLElement;
		if (target.hasPointerCapture(e.pointerId)) target.releasePointerCapture(e.pointerId);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (pending || deck.stable_id === null) return;
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
		bind:this={trackEl}
		class="rb-fader"
		role="slider"
		title="Tempo (pitch) - drags the deck's playback speed within its selected pitch range. Centre is 0%, the track's original tempo. Double-click resets to 0%."
		aria-label={`deck ${deck.deck_id} pitch fader`}
		aria-orientation="vertical"
		aria-valuemin={-pitchRange}
		aria-valuemax={pitchRange}
		aria-valuenow={Number(((deck.pitch - 1) * 100).toFixed(2))}
		aria-disabled={pending || deck.stable_id === null}
		tabindex="0"
		data-performance-control="pitch"
		data-testid={`pitch-fader-deck-${deck.deck_id}`}
		data-takeover-ghost={takeoverGhost === null ? undefined : takeoverGhost.value}
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
		ondblclick={handleDoubleClick}
		onkeydown={handleKeyDown}
	>
		<div class="rb-fader-track"></div>
		<div class="center-tick"></div>
		{#if takeoverGhost !== null}
			<div class="rb-fader-ghost" style={`top: ${ghostTopPx}px;`}></div>
		{/if}
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
				data-testid={`pitch-range-${range}-deck-${deck.deck_id}`}
				aria-label={`pitch range ${range === 100 ? 'wide' : `${range} percent`} deck ${deck.deck_id}`}
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
	.rb-fader-ghost {
		position: absolute;
		left: 2px;
		right: 2px;
		height: 10px;
		border: 1px dashed #f2b84b;
		border-radius: 2px;
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
