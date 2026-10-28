<script lang="ts">
	// TOPVIEW-03: the Phase meter view. Takes the wavestack's place when the
	// top bar's Phase meter icon is lit: one arc gauge per deck showing how far
	// its beat is ahead of (right) or behind (left) the master deck's nearest
	// beat, with the offset in ms at the master tempo, for correcting drift by
	// eye. The master deck's own gauge says MASTER. Reads the shared engine
	// state and playhead clock and adds no frame loop of its own.
	import { DECK_IDS, deckStates } from './engine-accessor';
	import { phaseLocks } from '$lib/audio-engine/rust-phase-lock';
	import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
	import { beatSyncFold, deckEffectiveBpm } from '$lib/rb/audio-engine.svelte';
	import { phaseMeterFold } from '$lib/rb/phase-meter-fold';
	import type { DeckId } from '$lib/rb/deck-id';
	import { playheadMs } from '$lib/rb/playhead-display.svelte';
	import {
		phaseMeterReading,
		phaseMeterReadingText,
		phaseOffsetBeats,
		pointOnCircle,
		type PhaseMeterReading
	} from '$lib/rb/phase-view-math';

	/** The arc spans -0.5 beat (left end) to +0.5 beat (right end). */

	let { lessLayout }: { lessLayout: boolean } = $props();
	const ARC_SWEEP = 0.25; // turns either side of 12 o'clock
	const CX = 60;
	const CY = 58;
	const R = 44;

	const shown = $derived<DeckId[]>(lessLayout ? [1, 2] : [...DECK_IDS]);
	const masterId = $derived<DeckId | null>(DECK_IDS.find((id) => deckStates[id].is_master) ?? null);

	type MeterState =
		| { kind: 'empty' }
		| { kind: 'master' }
		| { kind: 'no-master' }
		| { kind: 'unmeasured' }
		| PhaseMeterReading;

	interface MeterDeck {
		id: DeckId;
		state: MeterState;
	}

	function _measure(id: DeckId): MeterState {
		const deck = deckStates[id];
		if (deck.stable_id === null) return { kind: 'empty' };
		if (id === masterId) return { kind: 'master' };
		if (masterId === null) return { kind: 'no-master' };
		const master = deckStates[masterId];
		const bpm = deckEffectiveBpm(masterId);
		// A follower Beat Sync locked at half or double time walks its grid at
		// that rate (Sol P1 4301475291); an unlocked deck has no fold, whatever
		// its tempo (Sol P1 4301847502). The lock is the active engine's (Sol P1
		// 4301959423).
		const fold = phaseMeterFold(rustMode.enabled, phaseLocks[id]?.normalization, () => beatSyncFold(id));
		const beats = phaseOffsetBeats(
			master.anlz?.beatgrid.beats ?? [],
			playheadMs(masterId, master) / 1000,
			deck.anlz?.beatgrid.beats ?? [],
			playheadMs(id, deck) / 1000,
			fold
		);
		if (beats === null) return { kind: 'unmeasured' };
		// The readout is in ms at the master tempo, so with no usable master
		// tempo (none, or not a positive number) it says so instead of quietly switching to beats (Sol P1 4302183122).
		return phaseMeterReading(beats, bpm);
	}

	const decks = $derived<MeterDeck[]>(shown.map((id) => ({ id, state: _measure(id) })));

	function arcPath(): string {
		const a = pointOnCircle(CX, CY, R, -ARC_SWEEP);
		const b = pointOnCircle(CX, CY, R, ARC_SWEEP);
		return `M ${a.x} ${a.y} A ${R} ${R} 0 0 1 ${b.x} ${b.y}`;
	}
	const ARC = arcPath();

	function needle(beats: number): { x: number; y: number } {
		return pointOnCircle(CX, CY, R, beats * 2 * ARC_SWEEP);
	}

	function readout(s: MeterState): string {
		switch (s.kind) {
			case 'empty':
				return 'empty';
			case 'master':
				return 'MASTER';
			case 'no-master':
				return 'no master';
			case 'unmeasured':
				return 'no grid';
			case 'no-tempo':
			case 'offset':
				return phaseMeterReadingText(s);
		}
	}

	function meterTitle(d: MeterDeck): string {
		const s = d.state;
		switch (s.kind) {
			case 'empty':
				return `Deck ${d.id}: no track loaded`;
			case 'master':
				return `Deck ${d.id} is the master deck; every other deck's meter measures against its beat`;
			case 'no-master':
				return 'No master deck, so there is no beat to measure against';
			case 'unmeasured':
				return `Deck ${d.id}: cannot measure (no beatgrid at this position on this deck or the master)`;
			case 'no-tempo':
				return (
					`Deck ${d.id}: the master deck's tempo is unavailable, so the offset cannot be given in ms. ` +
					`The needle still shows the beat offset (${s.beats >= 0 ? '+' : ''}${s.beats.toFixed(3)} beat); ` +
					`the ends of the arc are half a beat out.`
				);
			case 'offset':
				return (
					`Deck ${d.id} phase offset: how far its beat is ahead (+, needle right) or behind ` +
					`(-, needle left) the master deck's nearest beat, in ms at the master tempo ` +
					`(${s.beats >= 0 ? '+' : ''}${s.beats.toFixed(3)} beat). The ends of the arc are half a beat out.`
				);
		}
	}
</script>

<div class="phase-meter rb-phase-view" data-testid="phase-meter-view" role="group" aria-label="Phase meter">
	{#each decks as d (d.id)}
		{@const s = d.state}
		<div
			class="meter-cell"
			class:master={s.kind === 'master'}
			data-deck={d.id}
			data-testid={`phase-meter-deck-${d.id}`}
			title={meterTitle(d)}
		>
			<svg viewBox="0 0 120 72" aria-hidden="true">
				<path class="arc" d={ARC} />
				<line class="center" x1={CX} y1={CY - R - 5} x2={CX} y2={CY - R + 5} />
				{#if s.kind === 'offset' || s.kind === 'no-tempo'}
					{@const tip = needle(s.beats)}
					<line class="needle" class:off={Math.abs(s.beats) > 0.05} x1={CX} y1={CY} x2={tip.x} y2={tip.y} />
				{/if}
				<text class="deck-num" x={CX} y={CY + 6}>{d.id}</text>
			</svg>
			<span class="meter-readout" data-testid={`phase-meter-readout-${d.id}`}>{readout(s)}</span>
		</div>
	{/each}
</div>

<style>
	.phase-meter {
		grid-area: wavestack;
		display: grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(0, 1fr);
		gap: 8px;
		padding: 6px 12px;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-bg);
		font-family: var(--rb-font);
	}
	.meter-cell {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2px;
		min-height: 0;
		color: var(--rb-text);
	}
	.meter-cell svg {
		flex: 1 1 auto;
		min-height: 0;
		height: 100%;
		max-width: 100%;
	}
	.arc {
		fill: none;
		stroke: var(--rb-border);
		stroke-width: 6;
		stroke-linecap: round;
	}
	.center {
		stroke: var(--rb-green);
		stroke-width: 2;
	}
	.needle {
		stroke: var(--rb-green);
		stroke-width: 3;
		stroke-linecap: round;
	}
	.needle.off {
		stroke: var(--rb-orange);
	}
	.deck-num {
		fill: var(--rb-text-dim);
		font-size: 14px;
		text-anchor: middle;
	}
	.meter-readout {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.master .meter-readout {
		color: var(--rb-master-text);
	}
</style>
