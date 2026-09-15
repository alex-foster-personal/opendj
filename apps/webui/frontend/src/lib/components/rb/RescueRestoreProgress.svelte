<script lang="ts">
	import { rescueRestoreStatus } from '$lib/rb/performance-rescue-restore.svelte';

	const active = $derived(
		rescueRestoreStatus.phase === 'restoring' || rescueRestoreStatus.phase === 'resuming'
	);
	const deckCount = $derived(rescueRestoreStatus.playing_deck_ids.length);
</script>

{#if active && deckCount > 0}
	<div class="rescue-restore-progress" role="status" data-rescue-restore-phase={rescueRestoreStatus.phase}>
		<span title="Gig rescue playback restore is waiting for deck decode before simultaneous resume">
			Restoring {deckCount} deck{deckCount === 1 ? '' : 's'}
		</span>
		<span class="rescue-deck-ticks" title="Per-deck decode readiness during rescue restore">
			{#each rescueRestoreStatus.playing_deck_ids as deckId (deckId)}
				<span
					class="rescue-deck-tick"
					class:decoded={rescueRestoreStatus.per_deck[deckId] === 'decoded'}
					class:failed={rescueRestoreStatus.per_deck[deckId] === 'failed'}
					title={`Deck ${deckId}: ${rescueRestoreStatus.per_deck[deckId]}`}
					data-rescue-deck={deckId}
					data-rescue-deck-status={rescueRestoreStatus.per_deck[deckId]}>{deckId}</span
				>
			{/each}
		</span>
	</div>
{/if}

<style>
	.rescue-restore-progress {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.85rem;
		opacity: 0.9;
	}
	.rescue-deck-ticks {
		display: inline-flex;
		gap: 0.25rem;
	}
	.rescue-deck-tick {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.1rem;
		height: 1.1rem;
		border: 1px solid var(--border);
		border-radius: 2px;
		font-size: 0.75rem;
	}
	.rescue-deck-tick.decoded {
		background: var(--accent);
		color: #fff;
	}
	.rescue-deck-tick.failed {
		border-color: var(--danger);
		color: var(--danger);
	}
</style>
