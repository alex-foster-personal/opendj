<script lang="ts">
	// Jog readout dial (SCREENSHOT-SPEC 3): circular SVG dial with live BPM
	// large, pitch percent, pitch range, and a red position tick rotating
	// with playback position. Right column: real Q, SLIP, and MT state; AU /
	// MA remain explicitly inert.
	import { DECK_IDS, deckEffectiveBpm, deckStates } from '$lib/rb/audio-engine.svelte';
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import { isTempoLockedToMaster, playbackBpm } from '$lib/rb/beat-sync-math';
	import {
		JOG_WHEEL_FACE_RADIUS,
		PHASE_MARK_INNER_RADIUS,
		PHASE_MARK_OUTER_RADIUS,
		jogPhaseBeats,
		phaseBeatMarks,
		pqtzBarPhase
	} from '$lib/components/rb/wave/wave-math';
	import type { PhaseBeatMark } from '$lib/components/rb/wave/wave-math';
	import { gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { playheadMs } from '$lib/rb/playhead-display.svelte';
	import { tracePlayhead } from '$lib/rb/playhead-trace';
	import ControlExplainer from './ControlExplainer.svelte';
	import { readPalette, resolveStripWaveformKind } from '$lib/components/rb/wave/render';
	import {
		blitJogRadial,
		JOG_RADIAL_INNER_RADIUS,
		type JogRadialFrame,
		type JogRadialPalette,
		type StripVocals
	} from './jog-radial-render';

	let {
		deck,
		pitchRange,
		pending,
		onQuantize,
		onQuantizeGrid,
		onMasterTempo,
		onSlip
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
	} = $props();

	const DIAL_CSS_PX = 104;
	// null, not undefined: Svelte 5 sets an unmounted bind:this to null, and the
	// canvas lives inside {#if showRadialCanvas}, which a deck handoff, a track
	// reload or an HMR swap unmounts. Typing it `| undefined` let a `=== undefined`
	// guard pass null into getComputedStyle (soak tester, Mon 5 Oct 2026).
	let radialCanvas: HTMLCanvasElement | null = $state(null);
	// Same palette source as the deck rows (WaveRow.svelte): the .perf-root
	// --rb-wave-* vars, re-read when the theme, the waveform colour choice or
	// the skin swaps them. $state.raw so an idle deck still repaints on a swap.
	let radialPalette = $state.raw<JogRadialPalette | null>(null);

	const radialOn: boolean = $derived(uiPrefs.jog_radial_waveform);
	const previewBands = $derived(deck.anlz?.waveform.preview ?? null);
	const hasPreview: boolean = $derived(previewBands !== null && previewBands.length > 0);
	const radialVocals: StripVocals | null = $derived.by(() => {
		if (deck.anlz === null) return null;
		const raw = (deck.anlz as { vocals?: StripVocals }).vocals;
		return raw ?? null;
	});
	const radialDurationSec: number | null = $derived(
		deck.duration_ms === null || deck.duration_ms <= 0 ? null : deck.duration_ms / 1000
	);
	const showRadialCanvas: boolean = $derived(radialOn && hasPreview);

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
	// The position every rotating part of the dial is drawn from (phase marks,
	// progress trail): the shared per-deck playhead clock (ANIM-CLOCK-01), so the
	// marks turn smoothly and in phase with the strip; reported to the probe.
	const jogPositionMs: number = $derived(playheadMs(deck.deck_id, deck));
	$effect(() => {
		if (deck.audible) tracePlayhead('jog', deck.deck_id, jogPositionMs);
	});
	const dialCircumference = 2 * Math.PI * 46;
	const tickAngle: number = $derived(
		deck.duration_ms === null || deck.duration_ms <= 0
			? 0
			: Math.min(1, Math.max(0, jogPositionMs / deck.duration_ms)) * 360
	);

	// Pin 67a4ce88805f: an obviously-playing deck needs a fast white line
	// completing one revolution per "phase" (a PQTZ bar).
	//
	// Pin f19a1b2a455a corrects two things about that first cut. The phase
	// length was read straight off deck.quantize_grid_beats, whose shipped
	// default is 1 (player/state.svelte.ts), so the visual completed a
	// revolution every single BEAT, carrying one mark, instead of "once per
	// phase (4 beats default)";
	// jogPhaseBeats resolves that (and the unimplemented 'phase' sentinel)
	// to DEFAULT_PQTZ_BAR_BEATS while still honouring a chosen 4 or 8.
	// jogPositionMs is projected from the engine's output timestamp and freezes
	// when the deck pauses or its clock stalls, so this only moves with playback.
	const barBeats: number = $derived(jogPhaseBeats(deck.quantize_grid_beats));
	const barPhase: number | null = $derived(
		pqtzBarPhase(deck.anlz?.beatgrid.beats ?? [], Math.max(0, jogPositionMs / 1000), barBeats)
	);
	const phaseAngle: number = $derived((barPhase ?? 0) * 360);
	const phaseTitle: string = $derived(
		barPhase === null
			? 'PQTZ phase unavailable - phase visual parked at the downbeat'
			: `${Math.round(barPhase * 100)}% through the ${barBeats}-beat phase`
	);
	// Pin f19a1b2a455a: one WHITE rim mark per beat in the phase (4 marks
	// for a 4-beat phase), beat 1 thicker than the rest. The centre-crossing
	// black radial grid pin 67a4ce88805f's cut drew is gone - "remove the
	// spinning black line - looks bad, the white line is plenty ... no
	// spinning UI to overlap the central wheel". Marks are therefore drawn
	// in the annulus outside the r=40 wheel face, never into it.
	const phaseMarks: PhaseBeatMark[] = $derived(phaseBeatMarks(barBeats));
	// PHASE_MARK_INNER_RADIUS already carries half the thickest rotating
	// stroke, so a round line cap lands outside the face rather than 1.5
	// units inside it. The red position tick uses the same inner endpoint:
	// it is stroke-width 3 and rotates, so it is spinning UI under the same
	// "no overlap" requirement.
	const markOuterY = 50 - PHASE_MARK_OUTER_RADIUS;
	const markInnerY = 50 - PHASE_MARK_INNER_RADIUS;

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
	const gridInertTip: string = $derived(gridFeatureInertTip(deck));
	const qTitle: string = $derived(
		gridless
			? gridInertTip
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
	const dialTitle: string = $derived(
		[
			`Jog dial: live BPM ${bpmText}, pitch ${pitchText}, range ${rangeText}`,
			showRadialCanvas ? 'radial waveform on' : null,
			phaseTitle,
			offTempoTitle
		]
			.filter((part): part is string => part !== null)
			.join('. ')
	);

	$effect(() => {
		const c = radialCanvas;
		// Not mounted: no element to read vars from, so skip by design. The draw
		// effect below needs both a canvas and a palette, so it skips too.
		if (c === null) return;
		void uiPrefs.theme;
		void uiPrefs.wave_palette;
		void uiPrefs.ui_skin;
		radialPalette = readPalette(c); // throws if not under .perf-root
	});

	$effect(() => {
		const c = radialCanvas;
		const palette = radialPalette;
		if (c === null || palette === null) return;
		if (!showRadialCanvas || previewBands === null || deck.anlz === null) return;
		const ctx = c.getContext('2d');
		if (ctx === null) throw new Error('JogDial: radial canvas 2d context unavailable');
		const dpr = typeof window === 'undefined' ? 1 : window.devicePixelRatio ?? 1;
		const css = DIAL_CSS_PX;
		const backing = Math.round(css * dpr);
		if (c.width !== backing || c.height !== backing) {
			c.width = backing;
			c.height = backing;
		}
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		const frame: JogRadialFrame = {
			widthPx: css,
			heightPx: css,
			palette,
			kind: resolveStripWaveformKind(deck.anlz.waveform.kind, uiPrefs.waveform_design),
			preview: previewBands,
			vocals: radialVocals,
			durationSec: radialDurationSec
		};
		blitJogRadial(ctx, frame, previewBands, radialVocals, dpr);
	});
</script>

	<div class="jog" role="group" aria-label={`jog controls deck ${deck.deck_id}`}>
	<div
		class="dial-wrap"
		class:jog-off-tempo={offTempoTitle !== null}
		class:dial-playing={deck.audible}
		class:dial-radial-wave={showRadialCanvas}
		title={dialTitle}
	>
		{#if showRadialCanvas}
			<canvas
				class="radial-wave"
				bind:this={radialCanvas}
				data-testid={`jog-radial-waveform-deck-${deck.deck_id}`}
				width={DIAL_CSS_PX}
				height={DIAL_CSS_PX}
				aria-hidden="true"
			></canvas>
		{/if}
		<svg viewBox="0 0 100 100" class="dial" role="img" aria-label={`jog dial readout, ${phaseTitle}`}>
			{#if showRadialCanvas}
				<circle cx="50" cy="50" r="47" fill="none" stroke="#23282f" stroke-width="2.5" />
				<circle
					class="wheel-fill-inner"
					cx="50"
					cy="50"
					r={JOG_RADIAL_INNER_RADIUS}
					fill="#14171d"
					stroke="none"
				/>
				<circle
					class="wheel-fill"
					cx="50"
					cy="50"
					r={JOG_WHEEL_FACE_RADIUS}
					fill="none"
					stroke="#1a1e25"
					stroke-width="1"
				/>
			{:else}
				<circle cx="50" cy="50" r="47" fill="#0a0c0f" stroke="#23282f" stroke-width="2.5" />
				<circle
					class="wheel-fill"
					cx="50"
					cy="50"
					r={JOG_WHEEL_FACE_RADIUS}
					fill="#14171d"
					stroke="#1a1e25"
					stroke-width="1"
				/>
			{/if}
			{#if deck.audible}
				<g class="phase-marks" transform={`rotate(${phaseAngle} 50 50)`}>
					{#each phaseMarks as mark (mark.angleDeg)}
						<line
							class="phase-mark"
							class:downbeat={mark.isDownbeat}
							x1="50"
							y1={markOuterY}
							x2="50"
							y2={markInnerY}
							transform={`rotate(${mark.angleDeg} 50 50)`}
						/>
					{/each}
				</g>
			{/if}
			{#if deck.stable_id !== null}
				<circle
					class="progress-trail"
					cx="50" cy="50" r="46" fill="none" stroke="#fff" stroke-width="1.5"
					stroke-dasharray={`${(tickAngle / 360) * dialCircumference} ${dialCircumference}`}
					transform="rotate(-90 50 50)"
				/>
				<line class="progress-zero" x1="50" y1="3" x2="50" y2="13" stroke="#fff" stroke-opacity="0.3" stroke-width="1" />
				{#if !(radialOn && hasPreview)}
					<line
						class="position-tick"
						x1="50"
						y1={markOuterY}
						x2="50"
						y2={markInnerY}
						stroke="#d0342c"
						stroke-width="3"
						stroke-linecap="round"
						transform={`rotate(${tickAngle} 50 50)`}
					/>
				{:else}
					<!-- Radial waveform: track start sits at 12 o-clock and runs clockwise, so the
					     playhead is a red line across the bands at the same angle as the trail. -->
					<line
						class="radial-playhead"
						data-testid={`jog-radial-playhead-deck-${deck.deck_id}`}
						x1="50"
						y1={50 - JOG_RADIAL_INNER_RADIUS}
						x2="50"
						y2={markInnerY}
						stroke="#d0342c"
						stroke-width="2"
						stroke-linecap="round"
						transform={`rotate(${tickAngle} 50 50)`}
					/>
				{/if}
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
							title={`${beats}-beat snap grid${deck.quantize_grid_beats === beats ? ' (selected)' : ''}`}
							aria-label={`${beats}-beat snap grid deck ${deck.deck_id}`}
							onclick={async () => await onQuantizeGrid(beats)}
						>
							{beats}
						</button>
					{/each}
					<button
						class="q-grid-opt rb-inert"
						disabled
						data-testid={`quantize-grid-phase-deck-${deck.deck_id}`}
						title={plannedTitle('quantize-grid-phase')}
						aria-label={`match to phase length deck ${deck.deck_id} - ${plannedTitle('quantize-grid-phase')}`}
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
		<button class="rb-lit-button rb-inert" disabled title={plannedTitle('auto-cue')} aria-label={`auto cue deck ${deck.deck_id}`} data-testid={`auto-cue-deck-${deck.deck_id}`}>AU</button>
		<button class="rb-lit-button rb-inert" disabled title={plannedTitle('manual-source')} aria-label={`manual deck ${deck.deck_id}`} data-testid={`manual-deck-${deck.deck_id}`}>MA</button>
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
		position: relative;
		width: 104px;
		height: 104px;
		flex: 0 0 auto;
	}
	.radial-wave {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		pointer-events: none;
		z-index: 0;
	}
	.dial {
		position: relative;
		z-index: 1;
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
	/* Pin 67a4ce88805f: an audible deck's wheel flips to an off-white face
	 * with black/grey text so a playing deck reads unmistakably differently
	 * from a stopped one - "audible" is the committed OUTPUT state, never the
	 * requested transport state, so this cannot light up ahead of real sound.
	 * The phase geometry (grid + marker) is SVG-only: it moves solely when
	 * the presented position changes, with no CSS clock animation. */
	.dial-wrap.dial-playing .wheel-fill {
		fill: #f2f0e8;
		stroke: #fff;
	}
	/* DECKUX-02: keep the polar band (r=6..39) open over the canvas. The
	 * inner text disc is a translucent scrim, never an opaque cover: the
	 * waveform runs under the BPM text and the halo below keeps it legible.
	 * Playing-face CSS must not refill the full r=40 face over the waveform. */
	.dial-wrap.dial-radial-wave .wheel-fill {
		fill: none;
	}
	.dial-wrap.dial-radial-wave .wheel-fill-inner {
		fill: #14171d;
		fill-opacity: 0.5;
	}
	.dial-wrap.dial-radial-wave.dial-playing .wheel-fill {
		fill: none;
		stroke: #fff;
	}
	.dial-wrap.dial-radial-wave.dial-playing .wheel-fill-inner {
		fill: #f2f0e8;
		fill-opacity: 0.6;
	}
	.dial-wrap.dial-radial-wave .dial text {
		paint-order: stroke;
		stroke: #14171d;
		stroke-width: 2.5px;
		stroke-opacity: 0.85;
		stroke-linejoin: round;
	}
	.dial-wrap.dial-radial-wave.dial-playing .dial text {
		stroke: #f2f0e8;
	}
	/* Pin f19a1b2a455a: white rim marks only. .downbeat (beat 1) is thicker
	 * than the rest; nothing here reaches inside the r=40 wheel face. */
	.phase-mark {
		stroke: #fff;
		stroke-width: 1.25;
		stroke-linecap: round;
	}
	.phase-mark.downbeat {
		stroke-width: 3;
	}
	.dial-wrap.dial-playing .bpm {
		fill: #101216;
	}
	.dial-wrap.dial-playing .pitch {
		fill: #34383d;
	}
	.dial-wrap.dial-playing .range {
		fill: #62666b;
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
