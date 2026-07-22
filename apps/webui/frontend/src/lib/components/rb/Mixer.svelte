<script lang="ts">
	/**
	 * Build unit: mixer (COMPONENT-MAP 1.4). Center mixer column.
	 * 4 channel strips in screen order 3 1 2 4 (SCREENSHOT-SPEC 4):
	 * TRIM + HIGH/MID/LOW knobs, headphone CUE (inert), vertical fader,
	 * STEM label. Below: headphone MIX/LEVEL (inert), crossfader assign
	 * matrices (real) either side of the horizontal crossfader (real).
	 *
	 * Engine wiring: controls render the shared mixer read model and issue
	 * changes through the same typed command dispatcher as browser IPC.
	 * This keeps preset automation, agent control, audio truth, and visible
	 * knob/fader positions inseparable.
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

	function handleCue(deck: DeckId, enabled: boolean): void {
		void runPerformanceCommandFromUi({ type: 'channel_cue', deck, enabled });
	}

	function handleHeadphoneMix(value: number): void {
		void runPerformanceCommandFromUi({ type: 'headphone_mix', value });
	}

	function handleHeadphoneLevel(value: number): void {
		void runPerformanceCommandFromUi({ type: 'headphone_level', value });
	}

	function refreshHeadphoneOutputs(): void {
		void runPerformanceCommandFromUi({ type: 'headphone_outputs_refresh' });
	}

	function acquireHeadphoneOutput(): void {
		void runPerformanceCommandFromUi({ type: 'headphone_output_acquire' });
	}

	function selectHeadphoneOutput(device_id: string): void {
		void runPerformanceCommandFromUi({ type: 'headphone_output_select', device_id });
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
				cueEnabled={mixerState.channels[deck].cue_enabled}
				ontrim={(v) => handleTrim(deck, v)}
				oneq={(band, v) => handleEq(deck, band, v)}
				onfader={(v) => handleFader(deck, v)}
				oncue={(enabled) => handleCue(deck, enabled)}
			/>
		{/each}
	</div>
	<div class="lower">
		<div class="hp-row">
			<HeadphoneCluster
				state={mixerState.headphones}
				onmix={handleHeadphoneMix}
				onlevel={handleHeadphoneLevel}
				onrefresh={refreshHeadphoneOutputs}
				onacquire={acquireHeadphoneOutput}
				onselect={selectHeadphoneOutput}
			/>
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
