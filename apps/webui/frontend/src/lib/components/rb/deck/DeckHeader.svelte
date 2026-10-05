<script lang="ts">
	// Deck header row (SCREENSHOT-SPEC 3, COMPONENT-MAP 1.3): artwork thumb,
	// deck number, title/artist, BPM+KEY readout, remaining/elapsed clocks,
	// key badge + semitone nudge arrows, KEY SYNC,
	// BEAT SYNC and exclusive MASTER stacked at the right.
	import { deckArtworkUrl } from '$lib/rb/api-rb';
	import { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
	import { formatKeySyncDeltaText, keySyncNotationBullet } from '$lib/rb/key-sync-copy';
	import {
		DECK_IDS,
		deckEffectiveBpm,
		deckStates,
		keySyncPreview,
		keySyncStatus,
		pitchRanges,
		rateDeckTrack
	} from '$lib/rb/audio-engine.svelte';
	import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
	import { beatSyncGridWarning } from '$lib/rb/beat-sync-math';
	import { queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
	import { gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
	import { keyAtPlayheadNow } from '$lib/player/key-playhead-lazy.svelte';
	import { keySyncStatusTitle, type KeySyncStatus } from '$lib/player/key/key-sync-status';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import ControlExplainer from './ControlExplainer.svelte';
	import TempoEditModal from './TempoEditModal.svelte';
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
	let tempoEditAt: { x: number; y: number } | null = $state(null);
	const artSrc: string | null = $derived(
		deck.stable_id === null ? null : deckArtworkUrl(deck.stable_id, 'orig')
	);
	$effect(() => {
		// Reset the failure flag whenever the artwork target changes.
		void artSrc;
		artworkFailed = false;
	});

	const bpmText: string = $derived(deck.bpm === null ? '--.--' : deck.bpm.toFixed(2));
	const syncBounds = $derived(tempoBoundsFromPitchRange(pitchRanges[deckId]));
	// Beat Sync is inert when gridFeaturesInert is true (missing / failed /
	// static_grid_untrusted: true). A trusted multi-anchor own map is not
	// gridless; Q and Beat Sync stay live. Transport is still not gated.
	const gridless: boolean = $derived(gridFeaturesInert(deck));
	const gridInertTip: string = $derived(gridFeatureInertTip(deck));
	// BEATSYNC-GRID-02: an uneven or extrapolated grid is said out loud, on
	// the control it affects, before the DJ leans on the lock. Pure math over
	// the deck's real grid; null for a clean grid and for no grid at all.
	const gridWarning: string | null = $derived(
		gridless ? null : (beatSyncGridWarning(deck.anlz?.beatgrid.beats ?? [])?.message ?? null)
	);
	const beatSyncTitle: string = $derived(
		(gridless
			? gridInertTip
			: deck.beat_sync_enabled
				? 'BEAT SYNC ON - lock beat phase to the tempo MASTER (BAR phase, folding if it must)'
				: 'BEAT SYNC OFF - this deck keeps its own tempo and phase') +
			(gridWarning === null ? '' : ` | ${gridWarning}`)
	);
	const beatSyncBullets: readonly string[] = $derived([
		'Locks this deck to the MASTER beat grid (BAR: beat 1 aligns with 1, … 4 with 4 whenever the two tempos match outright).',
		`Needs a real PQTZ grid on both decks. BAR tempo must land in pitch range [${syncBounds.min}, ${syncBounds.max}] (default +-16%).`,
		'Outside that window sync cannot engage - button reverts; use pitch or pick a closer BPM.',
		'No grid on this track: the button is inert and transport runs unsynced.',
		'BAR prefers an exact match, and folds to half/double tempo rather than refusing when that is the only lock available - it warns in orange and stays locked.',
		'A locked deck is kept on the beat by tempo trims of at most 0.3%, and re-joined when it is more than 15 ms off. An orange ! means this track has an uneven beatgrid, so the lock may wander.'
	]);
	const masterMode = $derived(queryPerformanceState().master_mode);
	const masterTitle: string = $derived(
		deck.stable_id === null
			? 'no track loaded'
			: deck.is_master && masterMode === 'locked'
				? 'MASTER LOCKED - auto handoff is off; click to restore AUTO'
				: deck.is_master
					? 'MASTER AUTO - this deck is the tempo / key sync reference; click to lock'
					: 'MASTER - make this deck the tempo / key sync reference (locks AUTO)'
	);
	const masterBullets: readonly string[] = [
		'Only one MASTER at a time. Followers with Beat Sync lock phase to this deck.',
		'KEY SYNC also uses MASTER as the Camelot reference.',
		'BeatSyncMax keeps BAR phase lock across seeks when followers are synced.',
		'AUTO hands off to an on-air, Beat-Synced playing deck when this one pauses, ends, or leaves the mix. LOCKED stays until you click MASTER again.'
	];
	const masterDataState = $derived(
		deck.is_master ? (masterMode === 'locked' ? 'locked' : 'auto') : 'off'
	);
	const masterAriaLabel = $derived(
		deck.is_master
			? masterMode === 'locked'
				? `master deck ${deckId} locked`
				: `master deck ${deckId} auto`
			: `master deck ${deckId}`
	);
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
	/** Show audible Camelot after KEY SYNC / nudge; playhead segments override metadata. */
	const keyPlayhead = $derived(
		keyAtPlayheadNow(deck.anlz, deck.position_ms, deck.key_shift_semitones, deck.key)
	);
	const keyText: string = $derived(keyPlayhead.display ?? '--');
	const masterEffectiveKey: string | null = $derived.by(() => {
		if (keySyncPlan === null) return null;
		const master = deckStates[keySyncPlan.masterDeck];
		const playhead = keyAtPlayheadNow(
			master.anlz,
			master.position_ms,
			master.key_shift_semitones,
			master.key
		);
		const display = playhead.display;
		return display === null || display === '--' ? master.key : display;
	});
	const followerEffectiveKey: string | null = $derived(keyText === '--' ? null : keyText);
	const keySyncDeltaText: string | null = $derived.by(() => {
		if (keySyncPlan === null) return null;
		return formatKeySyncDeltaText(
			followerEffectiveKey,
			masterEffectiveKey,
			keySyncPlan.deltaSemitones
		);
	});
	// Lit only while the follow really holds (DECKUX-34): an arm that is
	// waiting on a master reads as ARMED, never as ON over a wrong key.
	// keySyncStatus reads only $state deckStates fields, so this tracks them.
	const keySyncState: KeySyncStatus = $derived(keySyncStatus(deckId));
	const keySyncTitle: string = $derived(
		keySyncState === 'following'
			? 'KEY SYNC ON - this deck follows the selected master key'
			: keySyncState !== 'off'
				? keySyncStatusTitle(keySyncState)
				: !keySyncAvailable
					? 'requires a loaded Camelot-key master'
					: keySyncDeltaText === null
						? 'KEY SYNC OFF - exact target is unavailable'
						: `KEY SYNC OFF - ${keySyncDeltaText}`
	);
	const keySyncBullets: readonly string[] = $derived.by(() => {
		if (keySyncPlan === null) {
			return ['Load a parseable Camelot-key master and wait for the presented audio state.'];
		}
		const bullets = [
			`Target manual shift: ${keySyncPlan.targetManualShiftSemitones >= 0 ? '+' : ''}${keySyncPlan.targetManualShiftSemitones} semitones.`,
			'Click KEY SYNC to apply this exact listener-facing target.'
		];
		const notation = keySyncNotationBullet(followerEffectiveKey, masterEffectiveKey);
		if (notation !== null) bullets.push(notation);
		return bullets;
	});
	const keySyncWarning: string | null = $derived(
		!deck.master_tempo_enabled
			? 'Master Tempo is OFF. Tempo changes affect vocal pitch; KEY SYNC itself does not change playback speed.'
			: null
	);
	const keyColor: string | null = $derived(camelotKeyColor(keyText === '--' ? null : keyText));
	const keyHover: string | null = $derived.by(() => {
		const effective = keyText === '--' ? null : keyText;
		const crossNotation = camelotKeyHoverLabel(effective);
		const segmentNote = keyPlayhead.title;
		let base: string | null;
		if (segmentNote !== null) {
			base =
				crossNotation === null ? segmentNote : `${crossNotation} — ${segmentNote}`;
		} else {
			base = crossNotation;
		}
		const raw = deck.key;
		if (raw === null || deck.key_shift_semitones === 0) return base;
		const shift =
			deck.key_shift_semitones >= 0
				? `+${deck.key_shift_semitones}`
				: String(deck.key_shift_semitones);
		const suffix = ` (was ${raw}, shift ${shift})`;
		return base === null ? `${effective}${suffix}` : `${base}${suffix}`;
	});
	const origKeyHover: string | null = $derived(camelotKeyHoverLabel(deck.key));
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

	function onTempoReadoutDblClick(event: MouseEvent): void {
		if (deck.stable_id === null || deckEffectiveBpm(deckId) === null) return;
		event.preventDefault();
		event.stopPropagation();
		tempoEditAt = { x: event.clientX, y: event.clientY };
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
	const keyOffTitle: string = $derived.by(() => {
		const base = `Key shift ${keyShiftText} semitones from the original key`;
		const currentNote = camelotKeyHoverLabel(followerEffectiveKey);
		const originalNote = camelotKeyHoverLabel(deck.key);
		if (currentNote === null && originalNote === null) return base;
		const parts = [base];
		if (currentNote !== null) parts.push(`Current: ${currentNote}`);
		if (originalNote !== null) parts.push(`Original: ${originalNote}`);
		return parts.join(' — ');
	});
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
					<span
						class="art placeholder"
						title="No artwork found: none in rekordbox, the file, a cover image in its folder, or online"
					></span>
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
							 (from <span
								style={origKeyColor !== null ? `color:${origKeyColor}` : undefined}
								title={origKeyHover ?? undefined}>{deck.key}</span>)
						</span>
					</span>
				{/if}
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<span
					class="bpm"
					data-testid={`tempo-readout-deck-${deckId}`}
					aria-label={`tempo readout deck ${deckId}`}
					ondblclick={onTempoReadoutDblClick}
				>{bpmText}</span>
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

			<ControlExplainer
				title={keySyncTitle}
				bullets={keySyncBullets}
				warning={keySyncWarning}
				action={keySyncWarning === null ? null : masterTempoAction}
			>
				<button
					class="rb-lit-button keysync"
					class:lit={keySyncState === 'following'}
					class:armed={keySyncState !== 'off' && keySyncState !== 'following'}
					disabled={pending || (!keySyncAvailable && !deck.key_sync_enabled)}
					aria-pressed={keySyncState === 'following'}
					data-performance-control="key-sync"
					data-testid={`key-sync-deck-${deckId}`}
					aria-label={`key sync deck ${deckId}`}
					data-state={keySyncState === 'following' ? 'on' : keySyncState === 'off' ? 'off' : 'armed'}
					data-key-sync-status={keySyncState}
					title={keySyncTitle}
					onclick={async () => await onKeySync()}
				>
					KEY SYNC
				</button>
			</ControlExplainer>

			<div class="sync-col">
				<ControlExplainer title={beatSyncTitle} bullets={beatSyncBullets} warning={gridWarning}>
					<button
						class="rb-lit-button"
						class:lit={deck.beat_sync_enabled && !gridless}
						class:grid-uneven={gridWarning !== null}
						data-grid-warning={gridWarning}
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
						{#if gridWarning !== null}
							<span
								class="grid-warn"
								data-testid={`beat-sync-grid-warning-deck-${deckId}`}
								title={gridWarning}>!</span
							>
						{/if}
					</button>
				</ControlExplainer>
				<ControlExplainer title={masterTitle} bullets={masterBullets}>
					<button
						class="rb-lit-button master-btn"
						class:lit={deck.is_master}
						class:locked={deck.is_master && masterMode === 'locked'}
						disabled={pending || deck.stable_id === null}
						aria-pressed={deck.is_master}
						data-performance-control="master"
						data-testid={`master-deck-${deckId}`}
						aria-label={masterAriaLabel}
						data-state={masterDataState}
						data-master-mode={masterMode}
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

{#if tempoEditAt !== null}
	<TempoEditModal
		deckId={deckId}
		x={tempoEditAt.x}
		y={tempoEditAt.y}
		onclose={() => (tempoEditAt = null)}
	/>
{/if}

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
	.keysync.armed {
		outline: 1px dashed var(--rb-text-dim);
		outline-offset: -2px;
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
	/* Uneven-grid marker: sits in the button's corner so the label, and the
	   header's layout, do not move when a track with a rough grid loads. */
	.sync-col .grid-uneven {
		position: relative;
		border-color: var(--rb-orange, #e8952a);
	}
	.grid-warn {
		position: absolute;
		top: -5px;
		right: -3px;
		min-width: 11px;
		padding: 0 2px;
		border-radius: 6px;
		background: var(--rb-orange, #e8952a);
		color: #111;
		font-size: 9px;
		font-weight: 700;
		line-height: 11px;
		text-align: center;
	}
	.master-btn.lit {
		color: var(--rb-master-ink);
		background: var(--rb-master);
		box-shadow: 0 0 6px color-mix(in srgb, var(--rb-master) 45%, transparent);
		border-color: color-mix(in srgb, var(--rb-master) 85%, var(--rb-text));
	}
	.master-btn.lit.locked {
		box-shadow:
			inset 0 0 0 1px color-mix(in srgb, var(--rb-master-ink) 55%, transparent),
			0 0 6px color-mix(in srgb, var(--rb-master) 45%, transparent);
	}
	.master-btn.lit.locked::after {
		content: 'LOCK';
		position: absolute;
		right: 2px;
		bottom: 1px;
		font-size: 7px;
		line-height: 1;
		letter-spacing: 0.04em;
		opacity: 0.85;
		pointer-events: none;
	}
	.master-btn {
		position: relative;
	}
</style>
