<script lang="ts">
	// Jog readout dial (SCREENSHOT-SPEC 3): circular SVG dial with live BPM
	// large, pitch percent, pitch range, and a red position tick rotating
	// with playback position. Right column: real Q, SLIP, and MT state; AU /
	// MA remain explicitly inert.
	import type { PitchRange } from '$lib/rb/audio-engine.svelte';
	import type { DeckState } from '$lib/rb/types';

	let {
		deck,
		pitchRange,
		pending,
		onQuantize,
		onMasterTempo,
		onSlip,
		inertTip
	}: {
		deck: DeckState;
		pitchRange: PitchRange;
		pending: boolean;
		onQuantize: () => Promise<void>;
		onMasterTempo: () => Promise<void>;
		onSlip: () => Promise<void>;
		inertTip: string;
	} = $props();

	// Live BPM = track BPM x playback ratio (ratio driven by the pitch fader).
	const liveBpm: number | null = $derived(deck.bpm === null ? null : deck.bpm * deck.pitch);
	const bpmText: string = $derived(liveBpm === null ? '--.--' : liveBpm.toFixed(2));
	const pitchText: string = $derived(`${((deck.pitch - 1) * 100).toFixed(1)}%`);
	// Range readout is REAL from the engine's per-deck pitchRanges store;
	// 100 renders as WIDE per SCREENSHOT-SPEC 3.
	const rangeText: string = $derived(pitchRange === 100 ? 'WIDE' : `+-${pitchRange}`);
	const tickAngle: number = $derived(
		deck.duration_ms === null || deck.duration_ms === 0
			? 0
			: (deck.position_ms / deck.duration_ms) * 360
	);
</script>

<div class="jog">
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

	<div class="side-buttons">
		<button
			class="rb-lit-button"
			class:lit={deck.quantize_enabled}
			disabled={pending}
			aria-pressed={deck.quantize_enabled}
			data-performance-control="quantize"
			data-state={deck.quantize_enabled ? 'on' : 'off'}
			title="toggle quantize"
			onclick={async () => await onQuantize()}
		>
			Q
		</button>
		<button
			class="rb-lit-button"
			class:lit={deck.slip_enabled}
			class:active={deck.slip_active}
			disabled={pending}
			aria-pressed={deck.slip_enabled}
			data-performance-control="slip"
			data-state={deck.slip_active ? 'active' : deck.slip_enabled ? 'armed' : 'off'}
			title="toggle SLIP mode"
			onclick={async () => await onSlip()}
		>
			SLIP
		</button>
		<button
			class="rb-lit-button"
			class:lit={deck.master_tempo_enabled}
			disabled={pending}
			aria-pressed={deck.master_tempo_enabled}
			data-performance-control="master-tempo"
			data-state={deck.master_tempo_enabled ? 'on' : 'off'}
			data-processor-state={deck.processor_error !== null
				? 'error'
				: deck.master_tempo_enabled
					? deck.stable_id === null
						? 'armed'
						: 'active'
					: 'bypass'}
			title="toggle Master Tempo"
			onclick={async () => await onMasterTempo()}
		>
			MT
		</button>
		<button class="rb-lit-button rb-inert" disabled title={inertTip}>AU</button>
		<button class="rb-lit-button rb-inert" disabled title={inertTip}>MA</button>
		<button
			class="rb-lit-button small"
			class:lit={deck.master_tempo_enabled}
			disabled={pending}
			aria-pressed={deck.master_tempo_enabled}
			data-performance-control="master-tempo"
			data-state={deck.master_tempo_enabled ? 'on' : 'off'}
			title="toggle Master Tempo"
			onclick={async () => await onMasterTempo()}
		>
			MT
		</button>
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
	.dial {
		width: 104px;
		height: 104px;
		flex: 0 0 auto;
	}
	.dial text {
		text-anchor: middle;
		font-family: var(--rb-font);
		font-variant-numeric: tabular-nums;
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
	.side-buttons .small {
		font-size: 8px;
		padding: 1px 4px;
	}
</style>
