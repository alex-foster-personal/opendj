<script lang="ts">
	// LV1 Create pairing: exactly two decks, capture sync snapshot + edge.
	//
	// INERT (tooltip 'not implemented - see PARITY-TODO'): Capture, Align
	// hotcues, Reload sync. All three needed `/api/v1/pairings/sync-snapshots`
	// or `/api/v1/pairings/alignments`, which no daemon publishes - the capture
	// router and its repo were only ever parked on archive branches and never
	// routed into app.py, so every call 404'd. The deck picker below reads real
	// engine state and stays live; the three actions now fire no request at all.
	import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';

	let {
		open = $bindable(false)
	}: {
		open?: boolean;
	} = $props();

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

	let selected = $state<DeckId[]>([]);

	const loadedDecks = $derived(
		DECK_IDS.filter((d) => deckStates[d].stable_id !== null).map((d) => ({
			id: d,
			title: deckStates[d].title ?? deckStates[d].stable_id ?? `CH${d}`,
			playing: deckStates[d].playing,
			is_master: deckStates[d].is_master,
			stable_id: deckStates[d].stable_id as string
		}))
	);

	$effect(() => {
		if (!open) return;
		const playing = loadedDecks.filter((d) => d.playing).map((d) => d.id);
		selected = playing.length >= 2 ? playing.slice(0, 2) : loadedDecks.map((d) => d.id).slice(0, 2);
	});

	function _toggle(id: DeckId): void {
		if (selected.includes(id)) {
			selected = selected.filter((d) => d !== id);
			return;
		}
		if (selected.length >= 2) {
			selected = [selected[1], id];
			return;
		}
		selected = [...selected, id];
	}

</script>

{#if open}
	<div class="sheet" role="dialog" aria-label="Create pairing">
		<header>
			<strong>Create pairing</strong>
			<button type="button" class="x" onclick={() => (open = false)}>×</button>
		</header>
		<p class="hint">Select exactly two loaded decks. Playing decks are pre-checked.</p>
		<p class="hint">
			Capture, align and reload are inert: the pairings capture routes are not
			implemented - see PARITY-TODO.
		</p>
		<ul>
			{#each loadedDecks as d (d.id)}
				<li>
					<label>
						<input
							type="checkbox"
							checked={selected.includes(d.id)}
							onchange={() => _toggle(d.id)}
						/>
						CH{d.id}
						{#if d.is_master}<span class="tag">MASTER</span>{/if}
						{#if d.playing}<span class="tag play">PLAY</span>{/if}
						<span class="title">{d.title}</span>
					</label>
				</li>
			{:else}
				<li class="empty">No loaded decks</li>
			{/each}
		</ul>
		<footer>
			<button type="button" class="ghost rb-inert" disabled title={INERT_TITLE}>
				Align hotcues
			</button>
			<button type="button" class="ghost rb-inert" disabled title={INERT_TITLE}>
				Reload sync
			</button>
			<button type="button" class="primary rb-inert" disabled title={INERT_TITLE}>
				Capture
			</button>
		</footer>
	</div>
{/if}

<style>
	.sheet {
		position: fixed;
		top: 40px;
		right: 12px;
		z-index: 9500;
		width: min(360px, calc(100vw - 24px));
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-family: var(--rb-font, ui-sans-serif, system-ui, sans-serif);
		font-size: 12px;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 12px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
	}
	.x {
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 18px;
		cursor: pointer;
	}
	.hint {
		margin: 8px 12px;
		color: var(--rb-text-dim);
		font-size: 11px;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0 8px 8px;
		max-height: 240px;
		overflow: auto;
	}
	li {
		padding: 4px;
	}
	label {
		display: flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
	}
	.title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tag {
		font-size: 9px;
		padding: 0 4px;
		border-radius: 2px;
		background: #e0cc6e;
		color: #14171d;
	}
	.tag.play {
		background: var(--rb-green, #35c04f);
	}
	.empty {
		color: var(--rb-text-dim);
		padding: 12px;
	}
	footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		padding: 10px 12px;
		border-top: 1px solid var(--rb-border, #2a3038);
	}
	footer button {
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 4px 10px;
		font-size: 11px;
		cursor: pointer;
	}
	.ghost {
		background: transparent;
		color: var(--rb-text);
	}
	.primary {
		background: var(--rb-accent, #3d7dd9);
		border-color: var(--rb-accent, #3d7dd9);
		color: #fff;
	}
	footer button:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>
