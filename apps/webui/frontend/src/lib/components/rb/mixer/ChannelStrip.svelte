<script lang="ts">
	/**
	 * One mixer channel strip (SCREENSHOT-SPEC 4), top-down:
	 * channel number, TRIM, HI/MID/LOW, FILTER (visual stub),
	 * headphone CUE, vertical fader (fills remaining height), STEM label.
	 * Decks 3/4 render slightly lighter so 1/2 stay the visual focus.
	 */
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import { deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
	import type { DeckId, EqBand } from '$lib/rb/types';
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
</script>

<div
	class="strip"
	class:secondary
	class:deck-focus={focused}
	data-mixer-channel={deckId}
	onpointerenter={() => setHoveredDeck(deckId)}
	onpointerleave={() => {
		if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
	}}
>
	<span class="ch-num">{deckId}</span>
	<div class="trim-slot">
		<Knob label="TRIM" value={trim} tone="white" onchange={ontrim} />
	</div>
	<div class="eq-stack">
		<Knob label="HI" value={eqHigh} onchange={(v) => oneq('high', v)} />
		<Knob label="MID" value={eqMid} onchange={(v) => oneq('mid', v)} />
		<Knob label="LOW" value={eqLow} onchange={(v) => oneq('low', v)} />
	</div>
	<div class="filter-slot">
		<Knob label="FILTER" value={0.5} tone="rainbow" inert />
	</div>
	<button
		class:enabled={cueEnabled}
		class="cue-btn"
		aria-pressed={cueEnabled}
		onclick={() => oncue(!cueEnabled)}>CUE</button
	>
	<div class="fader-slot">
		<VFader
			value={fader}
			{playing}
			{looped}
			deckId={deckId}
			onchange={onfader}
			label={`channel ${deckId} fader`}
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
		padding: 2px 2px 1px;
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
		margin-top: 2px;
	}
</style>
