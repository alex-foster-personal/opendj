<script lang="ts">
	// Deck header row (SCREENSHOT-SPEC 3, COMPONENT-MAP 1.3): artwork thumb,
	// deck number, title/artist, BPM+KEY readout, remaining/elapsed clocks,
	// KEY SYNC (inert), key badge + nudge arrows (value real, nudges inert),
	// BEAT SYNC (inert) stacked over MASTER (inert, lit from deck state).
	import { artworkUrl } from '$lib/rb/api-rb';
	import type { DeckId, DeckState } from '$lib/rb/types';

	let {
		deck,
		deckId,
		inertTip
	}: { deck: DeckState; deckId: DeckId; inertTip: string } = $props();

	let artworkFailed: boolean = $state(false);
	const artSrc: string | null = $derived(
		deck.stable_id === null ? null : artworkUrl(deck.stable_id, 'm')
	);
	$effect(() => {
		// Reset the failure flag whenever the artwork target changes.
		void artSrc;
		artworkFailed = false;
	});

	const bpmText: string = $derived(deck.bpm === null ? '--.--' : deck.bpm.toFixed(2));
	const keyText: string = $derived(deck.key ?? '--');

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
</script>

<div class="deck-header">
	{#if artSrc !== null && !artworkFailed}
		<img
			class="art"
			src={artSrc}
			alt=""
			onerror={() => {
				// ARTWORK_NOT_FOUND -> grey placeholder slate, never a fake image.
				artworkFailed = true;
			}}
		/>
	{:else}
		<div class="art placeholder"></div>
	{/if}

	<span class="deck-num">{deckId}</span>

	<div class="meta" class:empty={deck.stable_id === null}>
		<span class="title">{deck.title ?? 'No track loaded'}</span>
		<span class="artist">{deck.artist ?? ''}</span>
	</div>

	<div class="readout">
		<span class="bpm">{bpmText}</span>
		<span class="key">{keyText}</span>
	</div>

	<div class="clocks">
		<span class="remain">{remainText}</span>
		<span class="elapsed">{elapsedText}</span>
	</div>

	<button class="rb-lit-button rb-inert keysync" disabled title={inertTip}>KEY SYNC</button>

	<div class="key-badge">
		<button class="nudge rb-inert" disabled title={inertTip}>&lt;</button>
		<span class="key-val">{keyText}</span>
		<span class="key-off">+-0</span>
		<button class="nudge rb-inert" disabled title={inertTip}>&gt;</button>
	</div>

	<div class="sync-col">
		<button class="rb-lit-button rb-inert" disabled title={inertTip}>BEAT SYNC</button>
		<button class="rb-lit-button rb-inert" class:lit={deck.is_master} disabled title={inertTip}>
			MASTER
		</button>
	</div>
</div>

<style>
	.deck-header {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		min-width: 0;
	}
	.art {
		width: 28px;
		height: 28px;
		flex: 0 0 28px;
		object-fit: cover;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
	}
	.art.placeholder {
		background: var(--rb-panel-raised);
	}
	.deck-num {
		font-size: var(--rb-fs-deck-title);
		font-weight: 700;
		color: var(--rb-text-dim);
	}
	.meta {
		display: flex;
		flex-direction: column;
		min-width: 0;
		flex: 1 1 auto;
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
	.readout {
		display: flex;
		align-items: baseline;
		gap: 4px;
		flex: 0 0 auto;
	}
	.readout .bpm {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.readout .key {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.clocks {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		flex: 0 0 auto;
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
</style>
