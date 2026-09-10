<script lang="ts">
	// Deck header row (SCREENSHOT-SPEC 3, COMPONENT-MAP 1.3): artwork thumb,
	// deck number, title/artist, BPM+KEY readout, remaining/elapsed clocks,
	// KEY SYNC, key badge + semitone nudge arrows,
	// BEAT SYNC and exclusive MASTER stacked at the right.
	import { artworkUrl } from '$lib/rb/api-rb';
	import { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
	import {
		DECK_IDS,
		deckStates,
		effectiveCamelotKey,
		keySyncPreview,
		pitchRanges,
		rateDeckTrack
	} from '$lib/rb/audio-engine.svelte';
	import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
	import { GRID_FEATURE_TIP, gridFeaturesInert } from '$lib/player/grid-features';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import ControlExplainer from './ControlExplainer.svelte';
	import RatingStars from '../browser/RatingStars.svelte';

	let {
		deck,
		deckId,
		pending,
		onBeatSync,
		onMaster,
		onMasterTempo,
		onKeySync,
		onKeyNudge,
		onResetTempo,
		onUnload,
		keySyncAvailable
	}: {
		deck: DeckState;
		deckId: DeckId;
		pending: boolean;
		onBeatSync: () => Promise<void>;
		onMaster: () => Promise<void>;
		onMasterTempo: () => Promise<void>;
		onKeySync: () => Promise<void>;
		onKeyNudge: (semitones: -1 | 1) => Promise<void>;
		onResetTempo: (ratio: number) => Promise<void>;
		onUnload: () => Promise<void>;
		keySyncAvailable: boolean;
	} = $props();

	let artworkFailed: boolean = $state(false);
	const artSrc: string | null = $derived(
		deck.stable_id === null ? null : artworkUrl(deck.stable_id, 'orig')
	);
	$effect(() => {
		// Reset the failure flag whenever the artwork target changes.
		void artSrc;
		artworkFailed = false;
	});

	const bpmText: string = $derived(deck.bpm === null ? '--.--' : deck.bpm.toFixed(2));
	const syncBounds = $derived(tempoBoundsFromPitchRange(pitchRanges[deckId]));
	// A loaded track with no real PQTZ grid has nothing to phase-lock with, so
	// BEAT SYNC is inert rather than lit-but-dead. Transport is deliberately
	// NOT gated the same way - play, pause and cue always run.
	const gridless: boolean = $derived(gridFeaturesInert(deck));
	const beatSyncTitle: string = $derived(
		gridless
			? GRID_FEATURE_TIP
			: deck.beat_sync_enabled
				? 'BEAT SYNC ON - lock beat phase to the tempo MASTER (BAR phase, folding if it must)'
				: 'BEAT SYNC OFF - this deck keeps its own tempo and phase'
	);
	const beatSyncBullets: readonly string[] = $derived([
		'Locks this deck to the MASTER beat grid (BAR: beat 1 aligns with 1, … 4 with 4 whenever the two tempos match outright).',
		`Needs a real PQTZ grid on both decks. BAR tempo must land in pitch range [${syncBounds.min}, ${syncBounds.max}] (default +-16%).`,
		'Outside that window sync cannot engage - button reverts; use pitch or pick a closer BPM.',
		'No grid on this track: the button is inert and transport runs unsynced.',
		'BAR prefers an exact match, and folds to half/double tempo rather than refusing when that is the only lock available - it warns in orange and stays locked.'
	]);
	const masterTitle: string = $derived(
		deck.stable_id === null
			? 'no track loaded'
			: deck.is_master
				? 'MASTER - this deck is the tempo / key sync reference'
				: 'MASTER - make this deck the tempo / key sync reference'
	);
	const masterBullets: readonly string[] = [
		'Only one MASTER at a time. Followers with Beat Sync lock phase to this deck.',
		'KEY SYNC also uses MASTER as the Camelot reference.',
		'BeatSyncMax keeps BAR phase lock across seeks when followers are synced.'
	];
	const keySyncPlan = $derived.by(() => {
		const source = deckStates[deckId];
		void source.key;
		void source.key_shift_semitones;
		void source.pitch;
		void source.master_tempo_enabled;
		void source.transport_pending;
		const masterId = DECK_IDS.find((candidate) => deckStates[candidate].is_master);
		if (masterId !== undefined) {
			const master = deckStates[masterId];
			void master.key;
			void master.key_shift_semitones;
			void master.pitch;
			void master.master_tempo_enabled;
			void master.transport_pending;
		}
		return keySyncPreview(deckId);
	});
	const keySyncDeltaText: string | null = $derived.by(() => {
		if (keySyncPlan === null) return null;
		if (keySyncPlan.deltaSemitones === 0) return 'already harmonically aligned';
		const magnitude = Math.abs(keySyncPlan.deltaSemitones);
		const direction = keySyncPlan.deltaSemitones > 0 ? 'up' : 'down';
		const vocalEffect = magnitude === 1 ? 'vocals slightly higher' : 'vocals much higher';
		const lowerVocalEffect = magnitude === 1 ? 'vocals slightly lower' : 'vocals much lower';
		return `${magnitude} semitone${magnitude === 1 ? '' : 's'} ${direction} - ${direction === 'up' ? vocalEffect : lowerVocalEffect}`;
	});
	const keySyncTitle: string = $derived(
		!keySyncAvailable
			? 'requires a loaded Camelot-key master'
			: deck.key_sync_enabled
				? 'KEY SYNC ON - this deck follows the selected master key'
				: keySyncDeltaText === null
					? 'KEY SYNC OFF - exact target is unavailable'
					: `KEY SYNC OFF - ${keySyncDeltaText}`
	);
	const keySyncBullets: readonly string[] = $derived(
		keySyncPlan === null
			? ['Load a parseable Camelot-key master and wait for the presented audio state.']
			: [
				`Target manual shift: ${keySyncPlan.targetManualShiftSemitones >= 0 ? '+' : ''}${keySyncPlan.targetManualShiftSemitones} semitones.`,
				'Click KEY SYNC to apply this exact listener-facing target.'
			]
	);
	const keySyncWarning: string | null = $derived(
		!deck.master_tempo_enabled
			? 'Master Tempo is OFF. Tempo changes affect vocal pitch; KEY SYNC itself does not change playback speed.'
			: null
	);
	/** Show audible Camelot after KEY SYNC / nudge; raw metadata stays in the tooltip. */
	const keyText: string = $derived(
		effectiveCamelotKey(deck.key, deck.key_shift_semitones) ?? deck.key ?? '--'
	);
	const keyColor: string | null = $derived(camelotKeyColor(keyText === '--' ? null : keyText));
	const keyHover: string | null = $derived.by(() => {
		const effective = keyText === '--' ? null : keyText;
		const base = camelotKeyHoverLabel(effective);
		const raw = deck.key;
		if (raw === null || deck.key_shift_semitones === 0) return base;
		const shift =
			deck.key_shift_semitones >= 0
				? `+${deck.key_shift_semitones}`
				: String(deck.key_shift_semitones);
		const suffix = ` (was ${raw}, shift ${shift})`;
		return base === null ? `${effective}${suffix}` : `${base}${suffix}`;
	});
	const keyShiftText: string = $derived(
		deck.key_shift_semitones >= 0
			? `+${deck.key_shift_semitones}`
			: String(deck.key_shift_semitones)
	);

	// --------------------------------------- tempo/key readout (pin 815937c87bc1)
	// Original-key colour is deck.key's OWN camelot colour, distinct from
	// keyColor above which tracks the shifted (current) key.
	const origKeyColor: string | null = $derived(camelotKeyColor(deck.key));
	const keyChanged: boolean = $derived(deck.key !== null && deck.key_shift_semitones !== 0);
	// Percentage offset matches PitchFader's own (pitch - 1) * 100 arithmetic.
	const tempoPct: number = $derived(Math.round((deck.pitch - 1) * 100));
	const tempoChanged: boolean = $derived(deck.pitch !== 1);
	const tempoPctText: string = $derived(tempoPct >= 0 ? `+${tempoPct}` : String(tempoPct));
	const resetTitle: string = $derived(
		deck.bpm === null
			? 'Reset to original tempo'
			: `Reset to original tempo: ${deck.bpm.toFixed(0)}bpm${deck.key !== null ? ` | key: ${deck.key}` : ''}`
	);
	const resetBullets: string[] = [
		'Tempo returns to the analysed 0% (the track\'s original bpm).',
		'Key returns to the analysed key by re-applying the existing single-semitone nudge back to 0 - there is no separate absolute key-reset command.'
	];

	/** Combined reset: tempo goes straight to ratio 1.0 (0%); key has no
	 * absolute-reset primitive in the engine, so it is walked back to 0
	 * semitones one nudge at a time through the SAME onKeyNudge the badge's
	 * arrows already use - no new IPC command type. */
	async function resetToOriginal(): Promise<void> {
		await onResetTempo(1);
		const shift = deck.key_shift_semitones;
		const step: -1 | 1 = shift > 0 ? -1 : 1;
		for (let i = 0; i < Math.abs(shift); i++) {
			await onKeyNudge(step);
		}
	}

	// ----------------------------------------------------------- _helpers

	function _fmtClock(ms: number): string {
		// MM:SS.d per SCREENSHOT-SPEC 3 (e.g. 00:00.0).
		const clamped = Math.max(0, ms);
		const totalS = clamped / 1000;
		const m = Math.floor(totalS / 60);
		const s = Math.floor(totalS % 60);
		const tenths = Math.floor((totalS * 10) % 10);
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${tenths}`;
	}

	const elapsedText: string = $derived(
		deck.duration_ms === null ? '--:--.-' : _fmtClock(deck.position_ms)
	);
	const remainText: string = $derived(
		deck.duration_ms === null ? '--:--.-' : `-${_fmtClock(deck.duration_ms - deck.position_ms)}`
	);
	const remainTitle: string = $derived(
		deck.duration_ms === null
			? 'Remaining time --:--.- (no track loaded)'
			: `Remaining time ${remainText} (MM:SS.d until the end of this track)`
	);
	const elapsedTitle: string = $derived(
		deck.duration_ms === null
			? 'Elapsed time --:--.- (no track loaded)'
			: `Elapsed time ${elapsedText} (MM:SS.d from the start of this track)`
	);
	const keyOffTitle: string = $derived(
		`Key shift ${keyShiftText} semitones from the original key`
	);
	const deckNumTitle: string = $derived(`Deck ${deckId}`);
</script>

<div class="deck-header">
	<div class="art-slot">
		{#if deck.stable_id !== null}
			<button
				type="button"
				class="art-btn"
				title="Unload deck"
				aria-label={`Unload deck ${deckId}`}
				data-testid={`unload-deck-${deckId}`}
				disabled={pending}
				onclick={() => void onUnload()}
			>
				{#if artSrc !== null && !artworkFailed}
					<img
						class="art"
						src={artSrc}
						alt=""
						onerror={() => {
							artworkFailed = true;
						}}
					/>
				{:else}
					<span class="art placeholder"></span>
				{/if}
				<span class="art-eject" aria-hidden="true">⏏</span>
			</button>
		{:else}
			<div class="art placeholder"></div>
		{/if}
	</div>

	<!-- Body wraps beside art so a second chrome row does not stack under the
	     full artwork height (header = max(art, body), not art + body). -->
	<div class="header-body">
		<span class="deck-num" title={deckNumTitle}>{deckId}</span>

		<div class="meta" class:empty={deck.stable_id === null}>
			<span class="title">{deck.title ?? 'No track loaded'}</span>
			<span class="artist">{deck.artist ?? ''}</span>
			{#if deck.stable_id !== null}
				<span class="deck-rating-row">
					<RatingStars rating={deck.rating} onrate={(n) => void rateDeckTrack(deckId, n)} />
					<!-- Dot only, per pin scope: the library column, the click-to-set
					     swatch selector, and the shift-stacked multi-tag layout are a
					     separate feature and stay out of this packet. -->
					<span class="color-dot" title="Color tag (not yet settable)"></span>
				</span>
			{/if}
		</div>

		{#snippet resetToOriginalAction()}
			<button
				type="button"
				class="reset-to-original"
				disabled={pending || deck.stable_id === null}
				aria-label={`reset deck ${deckId} to original tempo and key`}
				data-testid={`reset-to-original-deck-${deckId}`}
				onclick={async () => await resetToOriginal()}
			>
				{resetTitle}
			</button>
		{/snippet}
		<ControlExplainer
			title={resetTitle}
			bullets={resetBullets}
			action={deck.stable_id === null ? null : resetToOriginalAction}
			showDelayMs={150}
		>
			<div class="readout">
				{#if keyChanged}
					<span class="readout-key-line">
						<span style={keyColor !== null ? `color:${keyColor}` : undefined}>{keyText}</span>
						<span class="from">
							 (from <span style={origKeyColor !== null ? `color:${origKeyColor}` : undefined}>{deck.key}</span>)
						</span>
					</span>
				{/if}
				<span class="bpm">{bpmText}</span>
				{#if tempoChanged}
					<span class="readout-tempo-line">
						{tempoPctText}% <span class="from"> (from {deck.bpm?.toFixed(0) ?? '--'}bpm)</span>
					</span>
				{/if}
			</div>
		</ControlExplainer>

		<div class="clocks">
			<span class="remain" title={remainTitle}>{remainText}</span>
			<span class="elapsed" title={elapsedTitle}>{elapsedText}</span>
		</div>

		<div class="chrome">
			{#snippet masterTempoAction()}
				<button
					type="button"
					class="enable-master-tempo"
					disabled={pending}
					aria-label={`enable master tempo deck ${deckId}`}
					data-testid={`enable-master-tempo-deck-${deckId}`}
					onclick={async () => await onMasterTempo()}
				>
					Enable MT
				</button>
			{/snippet}
			<ControlExplainer
				title={keySyncTitle}
				bullets={keySyncBullets}
				warning={keySyncWarning}
				action={keySyncWarning === null ? null : masterTempoAction}
			>
				<button
					class="rb-lit-button keysync"
					class:lit={deck.key_sync_enabled}
					disabled={pending || !keySyncAvailable}
					aria-pressed={deck.key_sync_enabled}
					data-performance-control="key-sync"
					data-testid={`key-sync-deck-${deckId}`}
					aria-label={`key sync deck ${deckId}`}
					data-state={deck.key_sync_enabled ? 'on' : 'off'}
					title={keySyncTitle}
					onclick={async () => await onKeySync()}
				>
					KEY SYNC
				</button>
			</ControlExplainer>

			<div class="key-badge">
				<button
					class="nudge"
					disabled={pending || deck.stable_id === null || deck.key_shift_semitones === -12}
					data-performance-control="key-nudge-down"
					aria-label={`lower key by one semitone deck ${deckId}`}
					data-testid={`key-nudge-down-deck-${deckId}`}
					title="lower key by one semitone"
					onclick={async () => await onKeyNudge(-1)}
				>
					&lt;
				</button>
				<span
					class="key-val"
					style={keyColor !== null ? `color:${keyColor}` : undefined}
					title={keyHover ?? undefined}>{keyText}</span
				>
				<span class="key-off" title={keyOffTitle}>{keyShiftText}</span>
				<button
					class="nudge"
					disabled={pending || deck.stable_id === null || deck.key_shift_semitones === 12}
					data-performance-control="key-nudge-up"
					aria-label={`raise key by one semitone deck ${deckId}`}
					data-testid={`key-nudge-up-deck-${deckId}`}
					title="raise key by one semitone"
					onclick={async () => await onKeyNudge(1)}
				>
					&gt;
				</button>
			</div>

			<div class="sync-col">
				<ControlExplainer title={beatSyncTitle} bullets={beatSyncBullets}>
					<button
						class="rb-lit-button"
						class:lit={deck.beat_sync_enabled && !gridless}
						disabled={pending || gridless}
						aria-pressed={deck.beat_sync_enabled}
						data-performance-control="beat-sync"
						data-testid={`beat-sync-deck-${deckId}`}
						aria-label={`beat sync deck ${deckId}`}
						data-state={gridless ? 'inert' : deck.beat_sync_enabled ? 'on' : 'off'}
						title={beatSyncTitle}
						onclick={async () => await onBeatSync()}
					>
						BEAT SYNC
					</button>
				</ControlExplainer>
				<ControlExplainer title={masterTitle} bullets={masterBullets}>
					<button
						class="rb-lit-button master-btn"
						class:lit={deck.is_master}
						disabled={pending || deck.stable_id === null}
						aria-pressed={deck.is_master}
						data-performance-control="master"
						data-testid={`master-deck-${deckId}`}
						aria-label={`master deck ${deckId}`}
						data-state={deck.is_master ? 'on' : 'off'}
						title={masterTitle}
						onclick={async () => await onMaster()}
					>
						MASTER
					</button>
				</ControlExplainer>
			</div>
		</div>
	</div>
</div>

<style>
	.deck-header {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		min-width: 0;
		flex: 0 0 auto;
	}
	/* Fixed square slot with equal inset so the thumb is padded on all sides. */
	.art-slot {
		flex: 0 0 var(--rb-deck-art, 75px);
		width: var(--rb-deck-art, 75px);
		height: var(--rb-deck-art, 75px);
		box-sizing: border-box;
		padding: 4px;
		display: grid;
		place-items: stretch;
	}
	.art {
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		box-sizing: border-box;
	}
	.art.placeholder {
		background: var(--rb-panel-raised);
	}
	.header-body {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-start;
		align-content: flex-start;
		gap: 6px;
		row-gap: 4px;
		flex: 1 1 auto;
		min-width: 0;
	}
	.deck-num {
		font-size: var(--rb-fs-deck-title);
		font-weight: 700;
		color: var(--rb-text-dim);
		flex: 0 0 auto;
		line-height: 1.2;
		padding-top: 0;
		margin-top: -2px;
	}
	.art-btn {
		position: relative;
		display: block;
		padding: 0;
		border: none;
		background: transparent;
		cursor: pointer;
		width: 100%;
		height: 100%;
		min-width: 0;
		min-height: 0;
	}
	.art-btn .art {
		display: block;
	}
	.art-btn:hover .art {
		filter: brightness(0.45);
	}
	.art-eject {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 28px;
		color: #fff;
		opacity: 0;
		pointer-events: none;
		text-shadow: 0 1px 4px #000;
	}
	.art-btn:hover .art-eject {
		opacity: 1;
	}
	.meta {
		display: flex;
		flex-direction: column;
		justify-content: center;
		min-width: 0;
		flex: 1 1 64px;
		align-self: stretch;
		max-height: var(--rb-deck-art, 75px);
	}
	.meta.empty .title {
		color: var(--rb-text-dim);
		font-weight: 400;
	}
	.title {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.artist {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.deck-rating-row {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: var(--rb-fs-label);
	}
	/* Pin 54c59dd3f564: "color dot should be vertically center aligned with
	 * star icons and be 20% smaller." 9px -> 7.2px is the 20%.
	 *
	 * The centring is NOT what `align-items: center` above already does. That
	 * aligns LAYOUT BOXES, and those were already flush (both centred on
	 * 249.5 in a 1280x800 deck). What the maintainer can see is the INK: `.rb-star`
	 * carries `line-height: 1`, and the ☆ glyph paints low inside that line
	 * box, so the stars' ink centre sat 0.67px below the dot's. `top` here is
	 * that optical correction, in em of the STAR's font size rather than the
	 * row's, written as the SAME expression theme.css gives `.rb-star` so the
	 * two cannot drift apart. In this subtree it always resolves to the
	 * `--rb-fs-browser` fallback: `--rb-star-size` is a local custom property
	 * on TrackTable's `.c-rating` cell, and a deck header is never a
	 * descendant of one, so the narrowed-rating-column case cannot reach here.
	 * 0.045em is the measured 0.5px at that 11px default; residual 0.17px.
	 * Measured, not assumed -- performance-deck-color-dot.spec.ts reads the
	 * composited pixels back and fails if this is reverted. */
	.color-dot {
		display: inline-block;
		width: 7.2px;
		height: 7.2px;
		border-radius: 50%;
		border: 1px solid var(--rb-text-dim);
		background: transparent;
		font-size: var(--rb-star-size, var(--rb-fs-browser));
		position: relative;
		top: 0.045em;
	}
	.readout {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		flex: 0 1 auto;
		min-width: 0;
	}
	.readout .bpm {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.readout-key-line,
	.readout-tempo-line {
		font-size: 0.75em;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.readout .from {
		color: var(--rb-text-dim);
	}
	.reset-to-original {
		background: transparent;
		border: none;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 2px 4px;
		cursor: pointer;
	}
	.clocks {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		flex: 0 1 auto;
		min-width: 0;
		font-variant-numeric: tabular-nums;
	}
	.clocks .remain {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
	}
	.clocks .elapsed {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.keysync {
		flex: 0 0 auto;
	}
	.enable-master-tempo {
		border: 1px solid var(--rb-red);
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-red) 15%, var(--rb-panel-raised));
		color: var(--rb-red);
		font: inherit;
		font-weight: 650;
		padding: 3px 5px;
		cursor: pointer;
	}
	.enable-master-tempo:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.chrome {
		display: flex;
		flex-wrap: nowrap;
		align-items: flex-start;
		gap: 6px;
		flex: 0 0 auto;
		margin-left: auto;
	}
	.key-badge {
		display: flex;
		align-items: center;
		gap: 2px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 1px 3px;
		flex: 0 0 auto;
	}
	.key-badge .nudge {
		background: none;
		border: none;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		padding: 0 2px;
		cursor: default;
	}
	.key-badge .key-val {
		font-size: var(--rb-fs-label);
		font-weight: 600;
	}
	.key-badge .key-off {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.sync-col {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 0 0 auto;
	}
	.master-btn.lit {
		color: #1a1608;
		background: #c9b35a;
		box-shadow: 0 0 6px rgba(201, 179, 90, 0.45);
		border-color: #b8a24e;
	}
</style>
