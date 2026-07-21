<script lang="ts">
	// Build unit: deck (COMPONENT-MAP 1.3, SCREENSHOT-SPEC 3).
	// REAL: header meta + artwork, strip overview waveform click-to-seek,
	// hot-cue bank jumps, INT beat-loop cluster (engine.setLoop), CUE +
	// play/pause transport, jog dial readouts + position tick.
	// INERT (tooltip 'not implemented - see PARITY-TODO'): KEY SYNC, key
	// nudge arrows, BEAT SYNC, MASTER, HOT CUE dropdown, grid-adjust stacks,
	// Q, SLIP, MT, AU, MA, stems, pitch range.
	//
	// ALL live state comes from the audio-engine accessor: the engine unit
	// owns DeckState (types.ts) via the rune module audio-engine.svelte.ts.
	import { engine, getDeckState, pitchRanges } from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import type { DeckId, DeckState } from '$lib/rb/types';
	import DeckHeader from './deck/DeckHeader.svelte';
	import HotCueBank from './deck/HotCueBank.svelte';
	import JogDial from './deck/JogDial.svelte';
	import LoopCluster from './deck/LoopCluster.svelte';
	import StemRow from './deck/StemRow.svelte';
	import StripWaveform from './deck/StripWaveform.svelte';
	import TransportCluster from './deck/TransportCluster.svelte';

	let { deckId }: { deckId: DeckId } = $props();

	const deck: DeckState = $derived(getDeckState(deckId));
	const pitchRange: PitchRange = $derived(pitchRanges[deckId]);

	const INERT_TIP = 'not implemented - see PARITY-TODO';
	const PAD_LETTERS: string[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	// ------------------------------------------------- engine call plumbing
	// Engine methods throw loudly on empty decks (fail-fast contract);
	// callers below are gated by disabled states, never by silent catches.

	function seekTo(ms: number): void {
		engine.cueJump(deckId, ms);
	}

	function playPause(): void {
		if (deck.playing) {
			engine.pause(deckId);
		} else {
			engine.play(deckId);
		}
	}

	function returnToCue(): void {
		// Return-to-cue semantics (types.ts DeckState.cue_ms): stop transport
		// and jump to the cue point, track start when none is set.
		if (deck.playing) engine.pause(deckId);
		engine.cueJump(deckId, deck.cue_ms ?? 0);
	}

	function engageLoop(in_ms: number, out_ms: number): void {
		engine.setLoop(deckId, { in_ms, out_ms });
	}

	function disengageLoop(): void {
		engine.setLoop(deckId, null);
	}
</script>

<section class="rb-deck rb-panel" data-deck={deckId}>
	<!-- Performance pad letter strip along the panel top edge (static echo
	     of the hot-cue bank, SCREENSHOT-SPEC 3). -->
	<div class="pad-strip" aria-hidden="true">
		{#each PAD_LETTERS as letter, i (letter)}
			{#if i === 4}
				<span class="pad-sep">|</span>
			{/if}
			<span class="pad-letter">{letter}</span>
		{/each}
	</div>

	<DeckHeader {deck} {deckId} inertTip={INERT_TIP} />

	<StripWaveform {deck} onSeek={seekTo} />

	<div class="main-row">
		<!-- Left edge: 2 grid-adjust icon stacks (inert, COMPONENT-MAP 1.3). -->
		<div class="grid-adjust">
			<button class="rb-lit-button rb-inert" disabled title={INERT_TIP} aria-label="grid adjust">
				<span class="ticks">&#9475;&#9475;&#9475;</span>
			</button>
			<button class="rb-lit-button rb-inert" disabled title={INERT_TIP} aria-label="grid shift">
				<span class="ticks">&#9478;&#9478;&#9478;</span>
			</button>
		</div>

		<HotCueBank {deck} onJump={seekTo} inertTip={INERT_TIP} />

		<LoopCluster {deck} onEngage={engageLoop} onDisengage={disengageLoop} inertTip={INERT_TIP} />

		<TransportCluster {deck} onCue={returnToCue} onPlayPause={playPause} />

		<!-- Spacer pushes the jog cluster to the panel's right edge. -->
		<div class="spacer"></div>

		<JogDial {deck} {pitchRange} inertTip={INERT_TIP} />
	</div>

	<StemRow inertTip={INERT_TIP} />
</section>

<style>
	.rb-deck {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 6px 8px;
		min-height: 0;
		min-width: 0;
		flex: 1;
		overflow: hidden;
	}
	.pad-strip {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 10px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		line-height: 1;
	}
	.pad-sep {
		color: var(--rb-border);
	}
	.main-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1 1 auto;
		min-height: 0;
		min-width: 0;
	}
	.grid-adjust {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 0 0 auto;
	}
	.grid-adjust button {
		padding: 2px 3px;
	}
	.ticks {
		font-size: 8px;
		letter-spacing: 1px;
	}
	.spacer {
		flex: 1 1 0;
		min-width: 0;
	}
</style>
