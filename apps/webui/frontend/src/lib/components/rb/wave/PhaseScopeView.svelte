<script lang="ts">
	// TOPVIEW-02: the Phase scope view. Takes the wavestack's place when the
	// top bar's Phase scope icon is lit: one dial per deck, its dot turning
	// once per bar, so decks in phase turn together and a drifting deck reads
	// as a dot that leads or lags. Reads the shared engine state and the
	// shared playhead clock (ANIM-CLOCK-01), never a local copy, and adds no
	// animation frame loop of its own: it redraws on each published position,
	// the same cadence the jog dial's phase marks use.
	import { DECK_IDS, deckStates } from './engine-accessor';
	import { phaseLocks } from '$lib/audio-engine/rust-phase-lock';
	import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
	import { beatSyncFold } from '$lib/rb/audio-engine.svelte';
	import type { TempoNormalization } from '$lib/rb/beat-sync-math';
	import type { DeckId } from '$lib/rb/deck-id';
	import { phaseLockFold } from '$lib/rb/phase-meter-fold';
	import { playheadMs } from '$lib/rb/playhead-display.svelte';
	import { pointOnCircle, SCOPE_BAR_BEATS, scopeBarPhase, type BarPhase, type ScopeMaster } from '$lib/rb/phase-view-math';

	let { lessLayout }: { lessLayout: boolean } = $props();
	const BEAT_TICKS = Array.from({ length: SCOPE_BAR_BEATS }, (_, i) => i / SCOPE_BAR_BEATS);

	/** LESS mode shows decks 1 and 2 only, as the wavestack does. */
	const shown = $derived<DeckId[]>(lessLayout ? [1, 2] : [...DECK_IDS]);

	interface ScopeDeck {
		id: DeckId;
		loaded: boolean;
		isMaster: boolean;
		playing: boolean;
		/** Beat Sync's fold for this deck in the active engine; null when unlocked. */
		lock: TempoNormalization | null;
		bar: BarPhase | null;
	}

	const masterId = $derived<DeckId | null>(DECK_IDS.find((id) => deckStates[id].is_master) ?? null);

	/** The master's grid and playhead, which a locked follower is read against. */
	function scopeMaster(id: DeckId): ScopeMaster | null {
		if (masterId === null || masterId === id) return null;
		const master = deckStates[masterId];
		return { beats: master.anlz?.beatgrid.beats ?? [], positionSec: playheadMs(masterId, master) / 1000 };
	}

	const decks = $derived<ScopeDeck[]>(
		shown.map((id) => {
			const deck = deckStates[id];
			const beats = deck.anlz?.beatgrid.beats ?? [];
			const loaded = deck.stable_id !== null;
			// A follower Beat Sync locked, at any fold, is read at that fold, so it
			// turns with the master (Sol P1 4302401669), from the active engine's
			// lock, as the Phase meter reads it, and placed against the master's
			// bar rather than its own downbeat, whichever beats the join paired,
			// exact-tempo BEAT joins included (Sol P1 4302494490, 4349021851).
			const lock = phaseLockFold(rustMode.enabled, phaseLocks[id]?.normalization, () => beatSyncFold(id));
			return {
				id,
				loaded,
				isMaster: deck.is_master,
				playing: deck.playing,
				lock,
				bar: loaded ? scopeBarPhase(beats, playheadMs(id, deck) / 1000, lock, scopeMaster(id)) : null
			};
		})
	);

	function scopeTitle(d: ScopeDeck): string {
		if (!d.loaded) return `Deck ${d.id}: no track loaded`;
		if (d.bar === null) {
			return `Deck ${d.id}: no beatgrid at this position (before the first beat, after the last, or not analyzed)`;
		}
		const pct = Math.round(d.bar.phase * 100);
		return (
			`Deck ${d.id}${d.isMaster ? ' (master)' : ''}: beat ${d.bar.beatInBar} of ${SCOPE_BAR_BEATS}, ` +
			`${pct}% through the bar.` +
			(d.lock === null
				? ''
				: ` Beat Sync locked it${d.lock === 1 ? '' : ` at ${d.lock}x tempo`}, so it is read against the master's bar.`) +
			` The dot turns once per bar; decks in phase turn together.`
		);
	}
</script>

<div class="phase-scope rb-phase-view" data-testid="phase-scope-view" role="group" aria-label="Phase scope">
	{#each decks as d (d.id)}
		{@const dot = d.bar === null ? null : pointOnCircle(50, 50, 38, d.bar.phase)}
		<div
			class="scope-cell"
			class:master={d.isMaster}
			class:idle={!d.playing}
			data-deck={d.id}
			data-testid={`phase-scope-deck-${d.id}`}
			title={scopeTitle(d)}
		>
			<svg viewBox="0 0 100 100" aria-hidden="true">
				<circle class="ring" cx="50" cy="50" r="38" />
				{#each BEAT_TICKS as turn, i (turn)}
					{@const outer = pointOnCircle(50, 50, 46, turn)}
					{@const inner = pointOnCircle(50, 50, 40, turn)}
					<line class="tick" class:downbeat={i === 0} x1={inner.x} y1={inner.y} x2={outer.x} y2={outer.y} />
				{/each}
				{#if dot !== null}
					<line class="hand" x1="50" y1="50" x2={dot.x} y2={dot.y} />
					<circle class="dot" cx={dot.x} cy={dot.y} r="6" />
				{/if}
				<text class="deck-num" x="50" y="56">{d.id}</text>
			</svg>
			<span class="scope-label">
				{#if !d.loaded}
					empty
				{:else if d.bar === null}
					no grid
				{:else}
					{d.isMaster ? 'MASTER' : `beat ${d.bar.beatInBar}`}
				{/if}
			</span>
		</div>
	{/each}
</div>

<style>
	.phase-scope {
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
	.scope-cell {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2px;
		min-height: 0;
		color: var(--rb-text);
	}
	.scope-cell svg {
		flex: 1 1 auto;
		min-height: 0;
		height: 100%;
		max-width: 100%;
	}
	.ring {
		fill: none;
		stroke: var(--rb-border);
		stroke-width: 3;
	}
	.tick {
		stroke: var(--rb-text-dim);
		stroke-width: 2;
	}
	.tick.downbeat {
		stroke: var(--rb-red);
		stroke-width: 3;
	}
	.hand {
		stroke: var(--rb-accent);
		stroke-width: 2;
	}
	.dot {
		fill: var(--rb-accent);
	}
	.master .dot,
	.master .hand {
		fill: var(--rb-master);
		stroke: var(--rb-master);
	}
	.idle .dot,
	.idle .hand {
		opacity: 0.5;
	}
	.deck-num {
		fill: var(--rb-text-dim);
		font-size: 16px;
		text-anchor: middle;
	}
	.scope-label {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		letter-spacing: 0.04em;
	}
	.master .scope-label {
		color: var(--rb-master-text);
	}
</style>
