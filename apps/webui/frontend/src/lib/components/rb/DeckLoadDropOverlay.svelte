<script lang="ts">
	/**
	 * Four large channel drop targets shown while a library track is dragged.
	 * Sits over the deck area only - never covers the playlist tree.
	 */
	import { artworkUrl } from '$lib/rb/api-rb';
	import { deckStates, getDeckState } from '$lib/rb/audio-engine.svelte';
	import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import type { DeckId } from '$lib/rb/types';
	import {
		endTrackDrag,
		primaryDragStableId,
		TRACK_STABLE_MIME,
		trackDrag
	} from '$lib/rb/track-drag.svelte';

	/** Visual order mirrors the performance grid: 3|1 · 2|4 around the mixer. */
	const DROP_ORDER: DeckId[] = [3, 1, 2, 4];

	let hoverDeck = $state<DeckId | null>(null);

	function onOver(e: DragEvent, deck: DeckId): void {
		if (!e.dataTransfer?.types.includes(TRACK_STABLE_MIME)) return;
		e.preventDefault();
		e.dataTransfer.dropEffect = 'copy';
		hoverDeck = deck;
	}

	function onLeave(e: DragEvent, deck: DeckId): void {
		const next = e.relatedTarget;
		if (next instanceof Node && e.currentTarget instanceof Node && e.currentTarget.contains(next)) {
			return;
		}
		if (hoverDeck === deck) hoverDeck = null;
	}

	async function onDrop(e: DragEvent, deck: DeckId): Promise<void> {
		hoverDeck = null;
		const raw =
			e.dataTransfer?.getData(TRACK_STABLE_MIME)?.trim() ||
			primaryDragStableId() ||
			'';
		const stableId = raw.split(',')[0]?.trim() ?? '';
		endTrackDrag();
		if (stableId === '') return;
		e.preventDefault();
		try {
			const cur = getDeckState(deck);
			if (cur.stable_id !== null) {
				await dispatchPerformanceCommand({ type: 'unload', deck });
			}
			await dispatchPerformanceCommand({ type: 'load', deck, stable_id: stableId });
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`drop load CH${deck} failed: ${message}`, 'error');
		}
	}
</script>

{#if trackDrag.active}
	<div class="overlay" aria-label="drop track onto a channel">
		{#each DROP_ORDER as deckId (deckId)}
			{@const d = deckStates[deckId]}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="slot"
				class:hover={hoverDeck === deckId}
				class:empty={d.stable_id === null}
				data-drop-deck={deckId}
				ondragover={(e) => onOver(e, deckId)}
				ondragleave={(e) => onLeave(e, deckId)}
				ondrop={(e) => void onDrop(e, deckId)}
			>
				<span class="ch">CH{deckId}</span>
				{#if d.stable_id !== null}
					<img
						class="art"
						src={artworkUrl(d.stable_id, 's')}
						alt=""
						draggable="false"
						onerror={(e) => {
							(e.currentTarget as HTMLImageElement).style.visibility = 'hidden';
						}}
					/>
					<span class="meta">
						<span class="title">{d.title ?? d.stable_id}</span>
						<span class="artist">{d.artist ?? ''}</span>
					</span>
				{:else}
					<span class="empty-label">drop to load</span>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.overlay {
		position: absolute;
		inset: 0;
		z-index: 40;
		display: grid;
		grid-template-columns: 1fr 1fr 1fr 1fr;
		gap: 8px;
		padding: 10px 12px;
		pointer-events: none;
		background: color-mix(in srgb, #0a0c10 55%, transparent);
	}
	.slot {
		pointer-events: auto;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-height: 0;
		border: 2px dashed color-mix(in srgb, var(--rb-accent, #6af) 55%, #555);
		border-radius: 6px;
		background: color-mix(in srgb, #141820 88%, transparent);
		padding: 10px 8px;
		color: var(--rb-text, #ddd);
		font-family: var(--rb-font, system-ui);
	}
	.slot.hover {
		border-style: solid;
		border-color: #ff4da6;
		background: color-mix(in srgb, #ff4da6 18%, #141820);
		box-shadow: 0 0 0 1px #ff4da6;
	}
	.slot.empty {
		opacity: 0.92;
	}
	.ch {
		font-size: 11px;
		letter-spacing: 0.08em;
		color: var(--rb-text-dim, #9aa);
	}
	.art {
		width: 56px;
		height: 56px;
		object-fit: cover;
		border-radius: 3px;
		background: #222;
	}
	.meta {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		max-width: 100%;
		text-align: center;
	}
	.title,
	.artist {
		max-width: 140px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 11px;
	}
	.artist {
		color: var(--rb-text-dim, #9aa);
		font-size: 10px;
	}
	.empty-label {
		font-size: 11px;
		color: var(--rb-text-dim, #9aa);
	}
</style>
