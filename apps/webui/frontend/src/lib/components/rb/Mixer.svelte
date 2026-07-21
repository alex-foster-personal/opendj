<script lang="ts">
	/**
	 * Build unit: mixer (COMPONENT-MAP 1.4). Center mixer column.
	 * 4 channel strips in screen order 3 1 2 4 (SCREENSHOT-SPEC 4):
	 * TRIM + HIGH/MID/LOW knobs, headphone CUE (inert), vertical fader,
	 * STEM label. Below: headphone MIX/LEVEL (inert), crossfader assign
	 * matrices (real) either side of the horizontal crossfader (real).
	 *
	 * Engine wiring: real controls call the AudioEngine contract methods
	 * (types.ts). The implementation store is the audio-engine build unit's
	 * rune module at $lib/rb/audio-engine.svelte.ts (`.svelte.ts` REQUIRED,
	 * RECON-FRONTEND 10.1), expected export: `engine` implementing
	 * AudioEngine.
	 *
	 * UI knob/fader positions are local $state seeded at the contract
	 * defaults (knobs 0.5 = unity/flat, fader 1 = full, crossfader 0.5,
	 * decks 1+3 -> A, 2+4 -> B). The engine seeds the same defaults; values
	 * are only pushed on user interaction - no speculative engine calls at
	 * mount (AudioContext may not exist before a user gesture).
	 */
	import { engine } from '$lib/rb/audio-engine.svelte';
	import type { CrossfaderAssign, DeckId, EqBand } from '$lib/rb/types';
	import AssignMatrix from './mixer/AssignMatrix.svelte';
	import ChannelStrip from './mixer/ChannelStrip.svelte';
	import Crossfader from './mixer/Crossfader.svelte';
	import HeadphoneCluster from './mixer/HeadphoneCluster.svelte';

	/** Screen order of the strips, left to right (SCREENSHOT-SPEC 4). */
	const STRIP_ORDER: DeckId[] = [3, 1, 2, 4];

	interface ChannelUi {
		trim: number;
		eqHigh: number;
		eqMid: number;
		eqLow: number;
		fader: number;
	}

	function _defaultChannel(): ChannelUi {
		return { trim: 0.5, eqHigh: 0.5, eqMid: 0.5, eqLow: 0.5, fader: 1 };
	}

	let channels = $state<Record<DeckId, ChannelUi>>({
		1: _defaultChannel(),
		2: _defaultChannel(),
		3: _defaultChannel(),
		4: _defaultChannel()
	});
	let assigns = $state<Record<DeckId, CrossfaderAssign>>({ 1: 'A', 2: 'B', 3: 'A', 4: 'B' });
	let crossfader = $state(0.5);

	function handleTrim(deck: DeckId, value: number): void {
		channels[deck].trim = value;
		engine.setTrim(deck, value);
	}

	function handleEq(deck: DeckId, band: EqBand, value: number): void {
		if (band === 'high') {
			channels[deck].eqHigh = value;
		} else if (band === 'mid') {
			channels[deck].eqMid = value;
		} else if (band === 'low') {
			channels[deck].eqLow = value;
		}
		engine.setEq(deck, band, value);
	}

	function handleFader(deck: DeckId, value: number): void {
		channels[deck].fader = value;
		engine.setFader(deck, value);
	}

	function handleAssign(deck: DeckId, assign: CrossfaderAssign): void {
		assigns[deck] = assign;
		engine.assignChannel(deck, assign);
	}

	function handleCrossfader(value: number): void {
		crossfader = value;
		engine.setCrossfader(value);
	}
</script>

<section class="rb-mixer rb-panel">
	<div class="strips">
		{#each STRIP_ORDER as deck (deck)}
			<ChannelStrip
				deckId={deck}
				trim={channels[deck].trim}
				eqHigh={channels[deck].eqHigh}
				eqMid={channels[deck].eqMid}
				eqLow={channels[deck].eqLow}
				fader={channels[deck].fader}
				ontrim={(v) => handleTrim(deck, v)}
				oneq={(band, v) => handleEq(deck, band, v)}
				onfader={(v) => handleFader(deck, v)}
			/>
		{/each}
	</div>
	<div class="lower">
		<div class="hp-row">
			<HeadphoneCluster />
		</div>
		<div class="xfade-row">
			<AssignMatrix bus="A" {assigns} onassign={handleAssign} />
			<Crossfader value={crossfader} onchange={handleCrossfader} />
			<AssignMatrix bus="B" {assigns} onassign={handleAssign} />
		</div>
	</div>
</section>

<style>
	.rb-mixer {
		grid-area: mixer;
		display: flex;
		flex-direction: column;
		min-height: 0;
		padding: 6px 6px 4px;
		overflow: hidden;
	}
	.strips {
		flex: 1;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 2px;
		min-height: 0;
	}
	.lower {
		flex: 0 0 auto;
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding-top: 4px;
		border-top: 1px solid var(--rb-border);
	}
	.hp-row {
		display: flex;
		justify-content: flex-start;
	}
	.xfade-row {
		display: flex;
		align-items: center;
		gap: 5px;
	}
</style>
