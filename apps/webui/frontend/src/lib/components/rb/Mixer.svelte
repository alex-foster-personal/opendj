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
	import { onMount } from 'svelte';
	import { engine, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import {
		performanceCommandStatus,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { CrossfaderAssign, EqBand, HeadphoneAlignmentMode, HeadphoneOutputMode } from '$lib/rb/mixer-types';
	import { openCueAlignModal } from '$lib/rb/cue-align-session.svelte';
	import type { StemControl } from '$lib/rb/stem-types';
	import { setDeckLayoutMode, uiPrefs } from '$lib/rb/prefs.svelte';
	import AssignMatrix from './mixer/AssignMatrix.svelte';
	import ChannelStrip from './mixer/ChannelStrip.svelte';
	import Crossfader from './mixer/Crossfader.svelte';
	import HeadphoneCluster from './mixer/HeadphoneCluster.svelte';

	/** Screen order of the strips, left to right (SCREENSHOT-SPEC 4). Always
	 * 4 entries, MORE or LESS - pin 862cd3's LESS mode collapses strips 3/4
	 * (columns 0 and 3 below) to width 0 via CSS only; it never filters this
	 * array, so all four ChannelStrip instances - and their engine state -
	 * stay mounted. */
	const STRIP_ORDER: DeckId[] = [3, 1, 2, 4];

	/** Pin 862cd3: MORE/LESS two-deck performance layout, read from the
	 * persisted preference (also driven by Cmd/Ctrl+2/+4 via
	 * deck-layout-hotkeys.ts - same setter, single source of truth). */
	const deckLayoutLess = $derived(uiPrefs.deck_layout === 'less');

	// #1475 M enforcement: audio-engine.svelte.ts is a hotspot file already at
	// its frontend.max_fan_out ceiling, so it exposes setLevelCeiling(dbfs,
	// enabled) over primitives only and this component (already importing
	// both modules for deck layout) pushes the persisted calibration in,
	// rather than the engine importing prefs.svelte itself.
	$effect(() => {
		engine.setLevelCeiling(
			uiPrefs.level_calibration.ceiling_dbfs,
			uiPrefs.level_calibration.ceiling_enabled
		);
	});

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

	function handleFilter(deck: DeckId, value: number): void {
		void runPerformanceCommandFromUi({ type: 'filter', deck, value });
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

	async function handleStemMute(deck: DeckId, stem: StemControl): Promise<void> {
		const muted = getDeckState(deck).stems.controls[stem].muted;
		void runPerformanceCommandFromUi({ type: 'stem_mute', deck, stem, muted: !muted });
	}

	async function handleStemSolo(deck: DeckId, stem: StemControl): Promise<void> {
		const solo = getDeckState(deck).stems.controls[stem].solo;
		void runPerformanceCommandFromUi({ type: 'stem_solo', deck, stem, solo: !solo });
	}

	function handleStemEqMode(deck: DeckId, enabled: boolean): void {
		void runPerformanceCommandFromUi({ type: 'stem_eq_mode', deck, enabled });
	}

	function handleStemGain(deck: DeckId, stem: StemControl, value: number): void {
		void runPerformanceCommandFromUi({ type: 'stem_gain', deck, stem, value });
	}

	function handleHeadphoneMix(value: number): void {
		void runPerformanceCommandFromUi({ type: 'headphone_mix', value });
	}

	function handleHeadphoneLevel(value: number): void {
		void runPerformanceCommandFromUi({ type: 'headphone_level', value });
	}

	function handleHeadphoneOutputMode(mode: HeadphoneOutputMode): void {
		void runPerformanceCommandFromUi({ type: 'output_mode', mode });
	}

	function handleHeadDelay(value: number): void {
		void runPerformanceCommandFromUi({ type: 'head_delay_ms', value });
	}

	/** CUEOUT-14: the modal's mode radios; same command as POST /headphones/alignment-mode. */
	function handleAlignmentMode(value: HeadphoneAlignmentMode): void {
		void runPerformanceCommandFromUi({ type: 'headphone_alignment_mode', value });
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

	function selectMasterOutput(device_id: string): void {
		void runPerformanceCommandFromUi({ type: 'headphone_master_select', device_id });
	}

	function selectAudioInput(device_id: string): void {
		void runPerformanceCommandFromUi({ type: 'headphone_input_select', device_id });
	}

	onMount(() => {
		void runPerformanceCommandFromUi({ type: 'headphone_outputs_refresh' });
	});
</script>

<section class="rb-mixer rb-panel">
	<div class="deck-layout-toggle" role="group" aria-label="Deck layout">
		<button
			type="button"
			class="deck-layout-btn"
			class:active={!deckLayoutLess}
			title={deckLayoutLess
				? 'MORE - switch to all 4 decks (Cmd/Ctrl+4). Current layout is LESS (2 decks, more library space).'
				: 'MORE - currently showing all 4 decks. Click LESS for 2 decks and more library space (Cmd/Ctrl+2).'}
			onclick={() => setDeckLayoutMode('more')}
		>
			MORE
		</button>
		<button
			type="button"
			class="deck-layout-btn"
			class:active={deckLayoutLess}
			title={deckLayoutLess
				? 'LESS - currently showing 2 decks, more library space. Click MORE for all 4 decks (Cmd/Ctrl+4).'
				: 'LESS - switch to 2 decks and more library space (Cmd/Ctrl+2). Current layout is MORE (all 4 decks).'}
			onclick={() => setDeckLayoutMode('less')}
		>
			LESS
		</button>
	</div>
	<div class="strips" class:less={deckLayoutLess}>
		{#each STRIP_ORDER as deck (deck)}
			<!-- `inert`, not just `opacity: 0`. LESS mounts all four strips and
			     hides 3/4 with opacity plus pointer-events, and NEITHER removes
			     a descendant from sequential keyboard focus - so every control
			     in a collapsed strip has always been tabbable and arrow-key
			     operable while invisible. Pin 2917b0eca218 made that worse by
			     mounting FILTER in LESS too, but the hole predates it and
			     covers TRIM, the EQs, CUE, the fader and STEM as well. `inert`
			     removes the whole subtree from focus AND from the a11y tree,
			     so it closes all of them at once. Blinded review + Sol P2. -->
			<div
				class="strip-slot"
				class:collapsed={deckLayoutLess && (deck === 3 || deck === 4)}
				inert={deckLayoutLess && (deck === 3 || deck === 4)}
			>
				<ChannelStrip
					deckId={deck}
					less={deckLayoutLess}
					trim={mixerState.channels[deck].trim}
					eqHigh={mixerState.channels[deck].eq_high}
					eqMid={mixerState.channels[deck].eq_mid}
					eqLow={mixerState.channels[deck].eq_low}
					filter={mixerState.channels[deck].filter}
					fader={mixerState.channels[deck].fader}
					cueEnabled={mixerState.channels[deck].cue_enabled}
					stemEqMode={mixerState.channels[deck].stem_eq_mode}
					stemPending={performanceCommandStatus.deck_pending[deck] > 0}
					ontrim={(v) => handleTrim(deck, v)}
					oneq={(band, v) => handleEq(deck, band, v)}
					onfilter={(v) => handleFilter(deck, v)}
					onfader={(v) => handleFader(deck, v)}
					oncue={(enabled) => handleCue(deck, enabled)}
					onStemEqMode={(enabled) => handleStemEqMode(deck, enabled)}
					onStemGain={(stem, value) => handleStemGain(deck, stem, value)}
					onStemMute={(stem) => handleStemMute(deck, stem)}
					onStemSolo={(stem) => handleStemSolo(deck, stem)}
				/>
			</div>
		{/each}
	</div>
	<div class="lower">
		<div class="hp-row">
			<HeadphoneCluster
				state={mixerState.headphones}
				onmix={handleHeadphoneMix}
				onlevel={handleHeadphoneLevel}
				ondelay={handleHeadDelay}
				onrefresh={refreshHeadphoneOutputs}
				onacquire={acquireHeadphoneOutput}
				onselect={selectHeadphoneOutput}
				onmaster={selectMasterOutput}
				oninput={selectAudioInput}
				onmode={handleHeadphoneOutputMode}
				oncalibrate={openCueAlignModal}
				onAlignmentMode={handleAlignmentMode}
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
		/* clip, not hidden: a hidden box is still programmatically scrollable, and
		 * focusing a wide two-outputs headphone row scrolled it ~82px sideways. */
		overflow: clip;
	}
	/* Pin 246b0f5: "MORE/LESS toggle is too big ... pushing EQs down" -
	 * shrunk from padding-bottom 4px + 10px/2px-10px buttons (~22px tall)
	 * to ~14px, unconditionally (both modes - it is the same control in
	 * both, and MORE mode has no reason to keep the extra height either).
	 * channel-strip-less-floor.test.mjs derives the LESS deck-area floor
	 * from this height too. */
	.deck-layout-toggle {
		flex: 0 0 auto;
		display: flex;
		justify-content: center;
		gap: 2px;
		padding-bottom: 2px;
	}
	.deck-layout-btn {
		font-size: 9px;
		font-weight: 600;
		letter-spacing: 0.04em;
		padding: 1px 8px;
		border: 1px solid var(--rb-border);
		background: transparent;
		color: var(--rb-text-dim, #9aa4b2);
		cursor: pointer;
	}
	.deck-layout-btn:first-child {
		border-radius: 3px 0 0 3px;
		border-right: none;
	}
	.deck-layout-btn:last-child {
		border-radius: 0 3px 3px 0;
	}
	.deck-layout-btn.active {
		background: var(--rb-accent, #3d7bfd);
		color: #fff;
	}
	.strips {
		flex: 1;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 2px;
		min-height: 0;
		overflow: hidden;
		transition: grid-template-columns var(--rb-deck-layout-duration, 200ms) ease;
	}
	/* LESS: STRIP_ORDER is [3,1,2,4] - columns 0 (deck 3) and 3 (deck 4)
	 * collapse to 0, columns 1/2 (decks 1/2) expand into the released
	 * width. Chrome only - deck.less below hides visually, the strip
	 * component (and its engine wiring above) stays mounted. */
	.strips.less {
		grid-template-columns: 0 1fr 1fr 0;
	}
	.strip-slot {
		min-width: 0;
		overflow: hidden;
		transition: opacity var(--rb-deck-layout-duration, 200ms) ease;
	}
	.strip-slot.collapsed {
		opacity: 0;
		pointer-events: none;
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
		min-width: 0;
	}
	.xfade-row {
		display: flex;
		align-items: center;
		gap: 5px;
	}
</style>
