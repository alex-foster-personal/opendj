<script lang="ts">
	// TOPVIEW-05: the Track information overlay and its lazy-load failure. They
	// live here, mounted outside the top bar's evictable utility slot, because
	// the width tiers and LESS view hide that slot with display:none while an
	// agent's track_info_toggle still opens the record there: an overlay or
	// alert inside the slot would open invisibly (Sol P1 4350988015). The (i)
	// button itself (TrackInfoButton.svelte) stays in the slot.
	import type { Component } from 'svelte';
	import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/deck-id';
	import {
		closeTrackInfo,
		shownTrackInfo,
		trackInfo,
		type TrackInfoDeck
	} from '$lib/rb/track-info-state.svelte';

	let { onclose }: { onclose: () => void } = $props();

	let Overlay = $state<Component<{ deckId: DeckId; stableId: string; onclose: () => void }> | null>(
		null
	);

	const decks = $derived<TrackInfoDeck[]>(
		DECK_IDS.map((id) => ({
			id,
			loaded: deckStates[id].stable_id !== null,
			isMaster: deckStates[id].is_master,
			stableId: deckStates[id].stable_id
		}))
	);
	const shown = $derived(shownTrackInfo(decks));

	// The overlay's code loads on first open. Open state is set by the
	// dispatcher before the import, so a second click while the chunk loads
	// toggles it closed and the overlay never renders (Sol P1 4287872605).
	// A chunk that cannot be fetched (a tab left open across a deploy) closes
	// the overlay and shows the failure with reload guidance, so the click
	// never silently does nothing; the next open clears it and imports again
	// (Sol P2 4302079681). It is shown here rather than as a toast so this
	// component adds no importer of the toast store (fan-in ratchet).
	let loadFailure = $state<string | null>(null);

	$effect(() => {
		if (shown === null || Overlay !== null) return;
		loadFailure = null;
		import('./TrackInfoOverlay.svelte').then(
			(module) => {
				Overlay = module.default;
			},
			(err: unknown) => {
				closeTrackInfo();
				loadFailure = `Track information failed to load: ${err instanceof Error ? err.message : String(err)}. Reload the page to retry.`;
			}
		);
	});

	// The deck the record was opened for emptying or loading another track
	// closes the overlay, rather than switching it to whichever deck is focused
	// next or to that deck's new track.
	$effect(() => {
		if (trackInfo.opened !== null && shown === null) closeTrackInfo();
	});
</script>

{#if loadFailure !== null}
	<span class="tb-load-failure" role="alert" data-testid="topbar-track-info-load-failure">{loadFailure}</span>
{/if}
{#if Overlay !== null && shown !== null}
	<Overlay
		deckId={shown.deckId}
		stableId={shown.stableId}
		{onclose}
	/>
{/if}

<style>
	/* Fixed, not anchored to the (i) button: the button's slot can be hidden. */
	.tb-load-failure {
		position: fixed;
		top: 40px;
		right: 8px;
		z-index: 20;
		max-width: 280px;
		padding: 4px 6px;
		border: 1px solid var(--rb-red);
		border-radius: 2px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.3;
		white-space: normal;
	}
</style>
