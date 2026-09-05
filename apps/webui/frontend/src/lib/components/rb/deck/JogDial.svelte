<script lang="ts">
	// Jog readout dial (SCREENSHOT-SPEC 3): circular SVG dial with live BPM
	// large, pitch percent, pitch range, and a red position tick rotating
	// with playback position. Right column: real Q, SLIP, and MT state; AU /
	// MA remain explicitly inert.
	import { DECK_IDS, deckEffectiveBpm, deckStates } from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import { isTempoLockedToMaster, playbackBpm } from '$lib/rb/beat-sync-math';
	import { GRID_FEATURE_TIP, gridFeaturesInert } from '$lib/player/grid-features';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import ControlExplainer from './ControlExplainer.svelte';

	let {
		deck,
		pitchRange,
		pending,
		onQuantize,
		onQuantizeGrid,
		onMasterTempo,
		onSlip,
		inertTip
	}: {
		deck: DeckState;
		pitchRange: PitchRange;
		pending: boolean;
		onQuantize: () => Promise<void>;
		/** Pin a67bafbfc4b0: change the quantize GRID (1/4/8 beats). 'phase'
		 * (match to detected phase length) is NOT implemented - its option is
		 * greyed rb-inert and never calls this. */
		onQuantizeGrid: (beats: 1 | 4 | 8) => Promise<void>;
		onMasterTempo: () => Promise<void>;
		onSlip: () => Promise<void>;
		inertTip: string;
	} = $props();

	// Live BPM = PQTZ grid BPM x playback ratio (Beat Sync plans from PQTZ,
	// not rekordbox tag BPM - tag*pitch desyncs the dial after Bsync).
	const liveBpm: number | null = $derived(
		playbackBpm({
			beats: deck.anlz?.beatgrid.beats,
			positionSec: Math.max(0, deck.position_ms / 1000),
			tempoRatio: deck.pitch,
			tagBpm: deck.bpm
		})
	);
	const bpmText: string = $derived(liveBpm === null ? '--.--' : liveBpm.toFixed(2));

	/** Null while locked (or with no valid comparison to make); a numeric
	 * title otherwise so the tint always explains itself (house rule:
	 * numeric readouts carry an explanatory hover title). */
	function _offTempoTitle(candidateBpm: number | null, masterBpm: number | null): string | null {
		if (candidateBpm === null || masterBpm === null) return null;
		if (isTempoLockedToMaster(candidateBpm, masterBpm)) return null;
		return (
			`Off tempo: ${candidateBpm.toFixed(1)} BPM vs master ${masterBpm.toFixed(1)} BPM ` +
			`(not 1x/0.5x/2x locked)`
		);
	}

	// Off-tempo tint: read the elected master straight off the shared engine
	// state (deckStates), never a local copy, per AGENTS.md /performance
	// ("agents share the engine read model... never local copies").
	const masterBpm: number | null = $derived(
		(() => {
			const masterDeckId = DECK_IDS.find((candidate) => deckStates[candidate].is_master);
			return masterDeckId === undefined ? null : deckEffectiveBpm(masterDeckId);
		})()
	);
	const offTempoTitle: string | null = $derived(
		deck.is_master || !deck.audible ? null : _offTempoTitle(liveBpm, masterBpm)
	);
	const pitchText: string = $derived(`${((deck.pitch - 1) * 100).toFixed(1)}%`);
	// Range readout is REAL from the engine's per-deck pitchRanges store;
	// 100 renders as WIDE per SCREENSHOT-SPEC 3.
	const rangeText: string = $derived(pitchRange === 100 ? 'WIDE' : `+-${pitchRange}`);
	const tickAngle: number = $derived(
		deck.duration_ms === null || deck.duration_ms === 0
			? 0
			: (deck.position_ms / deck.duration_ms) * 360
	);

	const slipTitle: string = $derived(
		deck.slip_active
			? 'SLIP active - exit loop or turn off to jump to the hidden playhead'
			: deck.slip_enabled
				? 'SLIP armed - engage a loop to keep a hidden playhead advancing'
				: 'SLIP - arm so loops keep a hidden playhead advancing through the track'
	);
	const slipBullets: readonly string[] = [
		'Arm SLIP, then engage a loop (or arm while already looping).',
		'You hear the loop; a hidden playhead keeps advancing linearly.',
		'Exit the loop or turn SLIP off: jump to that hidden position.',
		'Pause clears active SLIP without jumping to the hidden position.'
	];
	// A loaded track with no real PQTZ grid has nothing to snap to, so Q is
	// inert rather than lying about what a click will do. Transport is
	// deliberately NOT gated the same way - play, pause and cue always run.
	const gridless: boolean = $derived(gridFeaturesInert(deck));
	const qTitle: string = $derived(
		gridless
			? GRID_FEATURE_TIP
			: deck.quantize_enabled
				? 'Quantize ON - snaps seeks, cue, and loop ends to the beatgrid'
				: 'Quantize OFF - seeks, cue, and loop ends use exact playhead times'
	);
	// Q label mirrors the active grid (pin a67bafbfc4b0). 'phase' is plumbed
	// but not implemented, so the label can show it (the setting exists) even
	// though nothing can select it into that state from this UI yet.
	const qLabel: string = $derived(
		deck.quantize_grid_beats === 'phase' ? 'Q-phase' : `Q${deck.quantize_grid_beats}`
	);
	const qGridBullets: readonly string[] = [
		'Choose the beat grid that seeks, cue points, and loop ends snap to.',
		'"match to phase length" is not implemented - it will match quantize to the detected phase length.'
	];
	const mtTitle: string = $derived(
		deck.master_tempo_enabled
			? 'Master Tempo ON - hold musical key while changing tempo'
			: 'Master Tempo OFF - pitch and key shift together with tempo'
	);
</script>

	<div class="jog" role="group" aria-label={`jog controls deck ${deck.deck_id}`}>
	<div class="dial-wrap" class:jog-off-tempo={offTempoTitle !== null} title={offTempoTitle ?? undefined}>
		<svg viewBox="0 0 100 100" class="dial" role="img" aria-label="jog dial readout">
			<circle cx="50" cy="50" r="47" fill="#0a0c0f" stroke="#23282f" stroke-width="2.5" />
			<circle cx="50" cy="50" r="40" fill="#14171d" stroke="#1a1e25" stroke-width="1" />
			{#if deck.stable_id !== null}
				<line
					x1="50"
					y1="4"
					x2="50"
					y2="12"
					stroke="#d0342c"
					stroke-width="3"
					stroke-linecap="round"
					transform={`rotate(${tickAngle} 50 50)`}
				/>
			{/if}
			<text x="50" y="47" class="bpm">{bpmText}</text>
			<text x="50" y="61" class="pitch">{pitchText}</text>
			<text x="50" y="72" class="range">{rangeText}</text>
		</svg>
	</div>

	<div class="side-buttons">
		<ControlExplainer title={qTitle} bullets={qGridBullets}>
			{#snippet action()}
				<div class="q-grid-options" role="group" aria-label={`quantize grid deck ${deck.deck_id}`}>
					{#each [1, 4, 8] as const as beats (beats)}
						<button
							class="q-grid-opt"
							class:selected={deck.quantize_grid_beats === beats}
							data-testid={`quantize-grid-${beats}-deck-${deck.deck_id}`}
							aria-pressed={deck.quantize_grid_beats === beats}
							onclick={async () => await onQuantizeGrid(beats)}
						>
							{beats}
						</button>
					{/each}
					<button
						class="q-grid-opt rb-inert"
						disabled
						data-testid={`quantize-grid-phase-deck-${deck.deck_id}`}
						title="not implemented, will match quantize to the detected phase length"
						aria-label="match to phase length - not implemented"
					>
						phase
					</button>
				</div>
			{/snippet}
			<button
				class="rb-lit-button"
				class:lit={deck.quantize_enabled && !gridless}
				disabled={pending || gridless}
				aria-pressed={deck.quantize_enabled}
				data-performance-control="quantize"
				data-testid={`quantize-deck-${deck.deck_id}`}
				aria-label={`quantize deck ${deck.deck_id}`}
				data-state={gridless ? 'inert' : deck.quantize_enabled ? 'on' : 'off'}
				title={qTitle}
				onclick={async () => await onQuantize()}
			>
				{qLabel}
			</button>
		</ControlExplainer>
		<ControlExplainer title={slipTitle} bullets={slipBullets} demo="slip">
			<button
				class="rb-lit-button"
				class:lit={deck.slip_enabled}
				class:active={deck.slip_active}
				disabled={pending}
				aria-pressed={deck.slip_enabled}
				data-performance-control="slip"
				data-testid={`slip-deck-${deck.deck_id}`}
				aria-label={`slip deck ${deck.deck_id}`}
				data-state={deck.slip_active ? 'active' : deck.slip_enabled ? 'armed' : 'off'}
				title={slipTitle}
				onclick={async () => await onSlip()}
			>
				SLIP
			</button>
		</ControlExplainer>
		<button
			class="rb-lit-button"
			class:lit={deck.master_tempo_enabled}
			disabled={pending}
			aria-pressed={deck.master_tempo_enabled}
			data-performance-control="master-tempo"
			data-testid={`master-tempo-deck-${deck.deck_id}`}
			aria-label={`master tempo deck ${deck.deck_id}`}
			data-state={deck.master_tempo_enabled ? 'on' : 'off'}
			data-processor-state={deck.processor_error !== null
				? 'error'
				: deck.master_tempo_enabled
					? deck.stable_id === null
						? 'armed'
						: 'active'
					: 'bypass'}
			title={mtTitle}
			onclick={async () => await onMasterTempo()}
		>
			MT
		</button>
		<button class="rb-lit-button rb-inert" disabled title={inertTip} aria-label={`auto cue deck ${deck.deck_id}`} data-testid={`auto-cue-deck-${deck.deck_id}`}>AU</button>
		<button class="rb-lit-button rb-inert" disabled title={inertTip} aria-label={`manual deck ${deck.deck_id}`} data-testid={`manual-deck-${deck.deck_id}`}>MA</button>
	</div>
</div>

<style>
	.jog {
		display: flex;
		align-items: center;
		gap: 6px;
		flex: 0 0 auto;
		min-height: 0;
	}
	.dial-wrap {
		width: 104px;
		height: 104px;
		flex: 0 0 auto;
	}
	.dial {
		width: 100%;
		height: 100%;
	}
	.dial text {
		text-anchor: middle;
		font-family: var(--rb-font);
		font-variant-numeric: tabular-nums;
	}
	/* Off-tempo tint: recolor the outer ring and add a soft glow. A CSS
	 * rule always outranks the ring's own stroke presentation attribute,
	 * so no extra markup is needed to override it. */
	.dial-wrap.jog-off-tempo .dial {
		filter: drop-shadow(0 0 4px var(--rb-red, #d0342c));
	}
	.dial-wrap.jog-off-tempo .dial circle:first-child {
		stroke: var(--rb-red, #d0342c);
		stroke-width: 3.5px;
	}
	.dial .bpm {
		fill: var(--rb-text);
		font-size: 15px;
		font-weight: 700;
	}
	.dial .pitch {
		fill: var(--rb-text);
		font-size: 9px;
	}
	.dial .range {
		fill: var(--rb-text-dim);
		font-size: 8px;
	}
	.side-buttons {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.side-buttons button {
		min-width: 34px;
		text-align: center;
	}
	.q-grid-options {
		display: flex;
		gap: 4px;
	}
	.q-grid-opt {
		min-width: 28px;
		padding: 3px 5px;
		background: #14171d;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		text-align: center;
		cursor: pointer;
	}
	.q-grid-opt.selected {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
</style>
