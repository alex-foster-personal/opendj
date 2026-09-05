<script lang="ts">
	/**
	 * One mixer channel strip (SCREENSHOT-SPEC 4), top-down:
	 * channel number, TRIM, HI/MID/LOW, FILTER (visual stub),
	 * headphone CUE, vertical fader (fills remaining height), STEM label.
	 * Decks 3/4 render slightly lighter so 1/2 stay the visual focus.
	 */
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import { deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { EqBand } from '$lib/rb/mixer-types';
	import { knobId } from '$lib/rb/knob-control.svelte';
	import Knob from './Knob.svelte';
	import VFader from './VFader.svelte';

	interface Props {
		/** The deck this strip controls (screen order is 3 1 2 4). */
		deckId: DeckId;
		/** TRIM knob 0..1; 0.5 = unity. */
		trim: number;
		/** HIGH knob 0..1; 0.5 = flat. */
		eqHigh: number;
		/** MID knob 0..1; 0.5 = flat. */
		eqMid: number;
		/** LOW knob 0..1; 0.5 = flat. */
		eqLow: number;
		/** Channel fader 0..1; 1 = full. */
		fader: number;
		cueEnabled: boolean;
		ontrim: (value: number) => void;
		oneq: (band: EqBand, value: number) => void;
		onfader: (value: number) => void;
		oncue: (enabled: boolean) => void;
	}

	let { deckId, trim, eqHigh, eqMid, eqLow, fader, cueEnabled, ontrim, oneq, onfader, oncue }: Props =
		$props();

	/** Decks 3/4 are secondary; lighten strip so 1/2 draw the eye. */
	const secondary = $derived(deckId === 3 || deckId === 4);
	const deck = $derived(getDeckState(deckId));
	const playing = $derived(deck.playing);
	const looped = $derived(deck.loop !== null && deck.loop.engaged);
	const focused = $derived(deckHoverUi.deckId === deckId);

	/** Halfway between TRIM's former 21px and the 30px EQ dials. */
	const TRIM_SIZE = 25.5;
	/** Current main owns this inert FILTER slot's presentation only; PR #492 owns the live COLOR-FX replacement. */
	const FILTER_SLOT_SIZE = 35.1;
</script>

<div
	class="strip"
	class:secondary
	class:deck-focus={focused}
	data-mixer-channel={deckId}
	role="group"
	aria-label={`channel ${deckId}`}
	onpointerenter={() => setHoveredDeck(deckId)}
	onpointerleave={() => {
		if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
	}}
>
	<span class="ch-num">{deckId}</span>
	<div class="trim-slot">
		<Knob
			knobId={knobId(deckId, 'trim')}
			label="TRIM"
			accessibleLabel={`trim deck ${deckId}`}
			value={trim}
			tone="white"
			size={TRIM_SIZE}
			onchange={ontrim}
		/>
	</div>
	<div class="eq-stack">
		<Knob knobId={knobId(deckId, 'high')} label="HI" accessibleLabel={`high EQ deck ${deckId}`} value={eqHigh} onchange={(v) => oneq('high', v)} />
		<Knob knobId={knobId(deckId, 'mid')} label="MID" accessibleLabel={`mid EQ deck ${deckId}`} value={eqMid} onchange={(v) => oneq('mid', v)} />
		<Knob knobId={knobId(deckId, 'low')} label="LOW" accessibleLabel={`low EQ deck ${deckId}`} value={eqLow} onchange={(v) => oneq('low', v)} />
	</div>
	<div class="filter-slot">
		<Knob
			knobId={knobId(deckId, 'filter')}
			label="FILTER"
			accessibleLabel={`filter deck ${deckId}`}
			value={0.5}
			tone="rainbow"
			size={FILTER_SLOT_SIZE}
			inert
		/>
	</div>
	<button
		class:enabled={cueEnabled}
		class="cue-btn"
		aria-pressed={cueEnabled}
		aria-label={`cue channel ${deckId}`}
		data-testid={`cue-channel-${deckId}`}
		onclick={() => oncue(!cueEnabled)}>CUE</button
	>
	<div class="fader-slot">
		<VFader
			value={fader}
			{playing}
			{looped}
			deckId={deckId}
			onchange={onfader}
			label={`channel fader deck ${deckId}`}
		/>
	</div>
	<span class="stem-label">STEM</span>
</div>

<style>
	.strip {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-start;
		gap: 2px;
		min-height: 0;
		height: 100%;
		/* Bottom pad opened up so STEM is not crowded against the strip's
		   lower border. The fader is `flex: 1 1 auto`, so the space comes out
		   of the channel level slider exactly as pin 8cd32a28c36d asks. */
		padding: 2px 2px 4px;
		border-radius: 2px;
	}
	.strip.secondary {
		background: color-mix(in srgb, var(--rb-panel-raised, #1a1e25) 55%, transparent);
	}
	.strip.deck-focus {
		transition:
			background 50ms ease-out,
			box-shadow 50ms ease-out;
		background: color-mix(in srgb, rgba(255, 255, 255, 0.1) 40%, var(--rb-panel-raised, #1a1e25));
		box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.22);
	}
	.ch-num {
		font-size: 10px;
		color: var(--rb-text);
		line-height: 1;
	}
	.trim-slot {
		margin-bottom: 7px;
	}
	.eq-stack {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
	}
	.filter-slot {
		margin-top: 7px;
		margin-bottom: 10px;
	}
	.cue-btn {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 8px;
		letter-spacing: 0.04em;
		padding: 2px 5px;
		line-height: 1;
		margin-top: 0;
		margin-bottom: 10px;
		flex: none;
		cursor: pointer;
	}
	.cue-btn.enabled {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.fader-slot {
		flex: 1 1 auto;
		min-height: 64px;
		width: 100%;
		display: flex;
		justify-content: center;
		align-items: stretch;
		margin-top: 0;
		margin-bottom: 8px;
	}
	.stem-label {
		font-size: 8px;
		letter-spacing: 0.06em;
		color: var(--rb-text-dim);
		line-height: 1;
		flex: none;
		/* 2px read as STEM touching the fader above it (pin 8cd32a28c36d). */
		margin-top: 5px;
	}
</style>
