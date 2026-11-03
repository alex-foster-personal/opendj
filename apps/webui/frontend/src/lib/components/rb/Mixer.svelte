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
	import { onMount, untrack } from 'svelte';
	import { listIoDevicesOnOpen, refreshIoDeviceList } from '$lib/rb/io-device-refresh';
	import { ioDeviceAccessOnOpen } from '$lib/player/io-device-access';
	import { ioSurface } from '$lib/rb/io-surface.svelte';
	import { engine, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import {
		performanceCommandStatus,
		runAudioIoTest,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { CrossfaderAssign, EqBand, HeadphoneAlignmentMode, HeadphoneOutputMode } from '$lib/rb/mixer-types';
	import { openCueAlignModal } from '$lib/rb/cue-align-session.svelte';
	import type { StemControl } from '$lib/rb/stem-types';
	import { setDeckLayoutMode, uiPrefs } from '$lib/rb/prefs.svelte';
	import { deckArtworkUrl } from '$lib/rb/artwork-url';
	import { librarySongCapacity } from '$lib/rb/library-song-capacity.svelte';
	import AssignMatrix from './mixer/AssignMatrix.svelte';
	import ChannelStrip from './mixer/ChannelStrip.svelte';
	import CrossfadeCurveSelect from './mixer/CrossfadeCurveSelect.svelte';
	import Crossfader from './mixer/Crossfader.svelte';
	import GroupHint from './mixer/GroupHint.svelte';
	import GroupPad from './mixer/GroupPad.svelte';
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

	/** Pin a00900579b7b: artwork that failed to load, keyed by stable id. */
	let artworkFailed = $state<ReadonlySet<string>>(new Set());

	function noteArtworkFailed(stableId: string): void {
		if (artworkFailed.has(stableId)) return;
		artworkFailed = new Set([...artworkFailed, stableId]);
	}

	/** Same play command as the deck transport, including Cmd/Ctrl quantize. */
	function duplicatePlayPause(deck: DeckId, event: MouseEvent): void {
		const deckState = getDeckState(deck);
		if (deckState.stable_id === null) return;
		const playing = !deckState.playing;
		const quantize = event.metaKey || event.ctrlKey;
		void runPerformanceCommandFromUi(
			{
				type: 'play',
				deck,
				playing,
				...(quantize && playing ? { quantize: true } : {})
			},
			event.timeStamp
		);
	}

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

	/** AUTOGAIN-04: the AG readout's write, through the same dispatcher as the knobs. */
	function handleAutoGainOffset(deck: DeckId, offsetDb: number | null, stableId: string) {
		return runPerformanceCommandFromUi({ type: 'auto_gain_offset', deck, offset_db: offsetDb, stable_id: stableId });
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

	function selectHeadphoneOutput(device_id: string): Promise<unknown> {
		return runPerformanceCommandFromUi({ type: 'headphone_output_select', device_id });
	}

	function selectMasterOutput(device_id: string): Promise<unknown> {
		return runPerformanceCommandFromUi({ type: 'headphone_master_select', device_id });
	}

	function selectAudioInput(device_id: string): Promise<unknown> {
		return runPerformanceCommandFromUi({ type: 'headphone_input_select', device_id });
	}

	// IOPIN-14: list the devices at mount and again whenever the I/O view opens.
	// Neither changes a route, so they bypass the command dispatcher: its session
	// does not exist yet at mount, which is how the old mount-time listing was
	// refused without a word. Rescan is the operator's own command and stays on
	// the dispatcher. Mount never asks for device access; whether OPEN may is the
	// build's one switch, set in vite.config.ts (dev server: ask once per origin;
	// every built app: only the grant button asks).
	const ioOpenMode = ioDeviceAccessOnOpen(import.meta.env.VITE_IO_DEVICE_ACCESS_ON_OPEN);
	onMount(() => void refreshIoDeviceList());
	$effect(() => {
		if (ioSurface.open) untrack(() => void listIoDevicesOnOpen(ioOpenMode));
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
	<div class="strips" class:less={deckLayoutLess} class:with-art={librarySongCapacity.showMixerColumnArt}>
		{#each STRIP_ORDER as deck (deck)}
			{@const channel = getDeckState(deck)}
			{@const artId = channel.stable_id}
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
				{#if librarySongCapacity.showMixerColumnArt}
					<div class="col-art" data-mixer-column-art={deck}>
						{#if artId !== null && !artworkFailed.has(artId)}
							<img
								class="col-art-img"
								src={deckArtworkUrl(artId, 's')}
								alt=""
								title="Track artwork"
								draggable="false"
								onerror={() => noteArtworkFailed(artId)}
							/>
						{:else}
							<div
								class="col-art-empty"
								title={artId === null ? 'No track loaded' : 'No artwork found'}
							></div>
						{/if}
						<button
							type="button"
							class="col-play"
							class:playing={channel.playing}
							disabled={artId === null}
							aria-label={`play deck ${deck}`}
							aria-pressed={channel.playing}
							title={artId === null ? 'no track loaded' : channel.playing ? 'Pause' : 'Play'}
							data-testid={`mixer-play-deck-${deck}`}
							onclick={(event) => duplicatePlayPause(deck, event)}
						>
							{#if channel.playing}
								<svg viewBox="0 0 16 16" class="col-play-glyph" aria-hidden="true">
									<rect x="3.5" y="3" width="3" height="10" fill="currentColor" />
									<rect x="9.5" y="3" width="3" height="10" fill="currentColor" />
								</svg>
							{:else}
								<svg viewBox="0 0 16 16" class="col-play-glyph" aria-hidden="true">
									<path d="M4.5 3 L13 8 L4.5 13 Z" fill="currentColor" />
								</svg>
							{/if}
						</button>
					</div>
				{/if}
				<div class="strip-fit">
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
					onAutoGainOffset={(db, id) => handleAutoGainOffset(deck, db, id)}
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
				testrun={runAudioIoTest}
			/>
		</div>
		<div class="xfade-row">
			<AssignMatrix bus="A" {assigns} onassign={handleAssign} />
			<CrossfadeCurveSelect />
			<Crossfader value={mixerState.crossfader} onchange={handleCrossfader} />
			<AssignMatrix bus="B" {assigns} onassign={handleAssign} />
		</div>
	</div>
	<GroupHint />
	<GroupPad />
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
		color: var(--rb-on-accent, #fff);
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
	/* Pin a00900579b7b: artwork (40) + gap (2) + play button (22) = 64, the
	 * same px +page.svelte adds to the deck row via --mixer-col-art-extra. */
	.strip-fit {
		min-width: 0;
		min-height: 0;
		height: 100%;
	}
	.strips.with-art .strip-slot {
		display: flex;
		flex-direction: column;
		height: 100%;
	}
	.strips.with-art .strip-fit {
		flex: 1 1 auto;
		min-height: 0;
		height: auto;
		display: flex;
		flex-direction: column;
	}
	.strips.with-art .strip-fit :global(.strip) {
		flex: 1 1 auto;
		height: auto;
		min-height: 0;
	}
	.col-art {
		flex: 0 0 var(--mixer-col-art-extra, 64px);
		height: var(--mixer-col-art-extra, 64px);
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-start;
		gap: 2px;
		overflow: hidden;
	}
	.col-art-img,
	.col-art-empty {
		width: 40px;
		height: 40px;
		display: block;
		border-radius: 2px;
		border: 1px solid var(--rb-border);
		background: #101318;
		object-fit: cover;
	}
	.col-play {
		width: 40px;
		height: 22px;
		padding: 0;
		border-radius: 3px;
		border: 1px solid var(--rb-border);
		background: #161b22;
		color: var(--rb-text);
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
	}
	.col-play:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.col-play.playing {
		color: var(--rb-accent, #3d7bfd);
		border-color: var(--rb-accent, #3d7bfd);
	}
	.col-play-glyph {
		width: 10px;
		height: 10px;
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
