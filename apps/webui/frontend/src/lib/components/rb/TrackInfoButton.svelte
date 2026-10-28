<script lang="ts">
	// TOPVIEW-05: the top bar's Track information (i) button. Opens the full
	// record for the focused deck: the one deck selected by a click on its
	// panel or mixer channel, else the master, else the lowest-numbered loaded
	// deck (focused-deck.ts). With no deck loaded it is disabled and says so.
	// Open state lives in track-info-state.svelte.ts and every click goes
	// through the performance dispatcher, which TopBar hands in as ontoggle,
	// so an agent's track_info_toggle and a click take the same path (Codex P1
	// 4300879440) and this button adds no dispatcher importer. The overlay and
	// its load failure render from TrackInfoHost.svelte, outside the evictable
	// slot this button sits in (Sol P1 4350988015).
	import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import {
		focusedTrackInfoDeck,
		shownTrackInfo,
		type TrackInfoDeck
	} from '$lib/rb/track-info-state.svelte';

	let { ontoggle }: { ontoggle: () => void } = $props();

	const decks = $derived<TrackInfoDeck[]>(
		DECK_IDS.map((id) => ({
			id,
			loaded: deckStates[id].stable_id !== null,
			isMaster: deckStates[id].is_master,
			stableId: deckStates[id].stable_id
		}))
	);
	const focused = $derived(focusedTrackInfoDeck(decks));
	const shown = $derived(shownTrackInfo(decks));

	const buttonTitle = $derived(
		focused === null
			? 'Track information - load a track onto a deck first; this opens the full record for the focused deck'
			: `Track information for deck ${focused} (the deck you selected, else the master): tags, provenance, play counts, file path`
	);
</script>


<span class="tb-info-wrap">
	<button
		type="button"
		class="tb-icon"
		class:active={shown !== null}
		data-testid="topbar-track-info"
		disabled={focused === null}
		aria-expanded={shown !== null}
		aria-haspopup="dialog"
		aria-label="track information"
		title={buttonTitle}
		onclick={ontoggle}
	>
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" />
			<rect x="5.3" y="5" width="1.4" height="4" fill="currentColor" />
			<rect x="5.3" y="2.6" width="1.4" height="1.4" fill="currentColor" />
		</svg>
	</button>
</span>

<style>
	/* Visual match for TopBar's .tb-icon without depending on its scoped styles. */
	.tb-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		padding: 2px 3px;
		line-height: 1;
		cursor: pointer;
	}
	.tb-icon:hover:not(:disabled) {
		color: var(--rb-text);
	}
	.tb-icon.active {
		color: var(--rb-accent);
	}
	.tb-info-wrap {
		position: relative;
		display: flex;
	}
	.tb-icon:disabled {
		cursor: default;
		opacity: 0.5;
	}
</style>
