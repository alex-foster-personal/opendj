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
	 * UI knob/fader positions read the same reactive engine state exposed by
	 * browser IPC, so human and agent control paths stay visibly identical.
	 */
	import { mixerState } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import type { CrossfaderAssign, DeckId, EqBand } from '$lib/rb/types';
	import AssignMatrix from './mixer/AssignMatrix.svelte';
	import ChannelStrip from './mixer/ChannelStrip.svelte';
	import Crossfader from './mixer/Crossfader.svelte';
	import HeadphoneCluster from './mixer/HeadphoneCluster.svelte';

	/** Screen order of the strips, left to right (SCREENSHOT-SPEC 4). */
	const STRIP_ORDER: DeckId[] = [3, 1, 2, 4];

	const assigns: Record<DeckId, CrossfaderAssign> = $derived({
		1: mixerState.channels[1].assign,
		2: mixerState.channels[2].assign,
		3: mixerState.channels[3].assign,
		4: mixerState.channels[4].assign
	});

	function handleTrim(deck: DeckId, value: number): void {
		void runPerformanceCommandFromUi({ type: 'trim', deck, value });
	}

	function handleEq(deck: DeckId, band: EqBand, value: number): void {
		void runPerformanceCommandFromUi({ type: 'eq', deck, band, value });
	}

	function handleFader(deck: DeckId, value: number): void {
		void runPerformanceCommandFromUi({ type: 'fader', deck, value });
	}

	function handleAssign(deck: DeckId, assign: CrossfaderAssign): void {
		void runPerformanceCommandFromUi({ type: 'assign', deck, assign });
	}

	function handleCrossfader(value: number): void {
		void runPerformanceCommandFromUi({ type: 'crossfader', value });
	}
</script>

<section class="rb-mixer rb-panel">
	<div class="strips">
		{#each STRIP_ORDER as deck (deck)}
			<ChannelStrip
				deckId={deck}
				trim={mixerState.channels[deck].trim}
				eqHigh={mixerState.channels[deck].eq_high}
				eqMid={mixerState.channels[deck].eq_mid}
				eqLow={mixerState.channels[deck].eq_low}
				fader={mixerState.channels[deck].fader}
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
			<Crossfader value={mixerState.crossfader} onchange={handleCrossfader} />
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
