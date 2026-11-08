<script lang="ts">
	// Compact track identity in a waveform gutter (DECKUX-40): artwork the full
	// row height, then the title over the live key and BPM. Missing or loading
	// artwork is empty space with a hover reason, never a labelled slate. The
	// markup lives in WaveTrackSummaryView.svelte so it can carry a story.
	import { coverFailed, coverLoaded, coverSrc, coverVisible } from '$lib/rb/artwork-cover.svelte';
	import { deckArtworkUrl } from '$lib/rb/artwork-url';
	import { playbackBpm } from '$lib/rb/beat-sync-math';
	import { camelotKeyColor } from '$lib/rb/camelot-color';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import { deckKeyAtPlayheadNow } from '$lib/player/key-playhead-lazy.svelte';
	import WaveTrackSummaryView from './WaveTrackSummaryView.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	const { deck }: { deck: DeckState } = $props();
	const fullTitle = $derived.by(() => {
		if (deck.stable_id === null) throw new Error('WaveTrackSummary mounted for an empty deck');
		if (deck.title === null) throw new Error(`loaded deck ${deck.stable_id} is missing a title`);
		return deck.title;
	});
	const artworkSrc = $derived(deck.stable_id === null ? null : deckArtworkUrl(deck.stable_id, 's'));

	// Key and tempo are read at whole-second resolution: position_ms moves
	// every frame, and a one-second bucket keeps the grid lookup off the
	// per-frame path (the derived below only re-runs when the bucket changes).
	const positionBucketSec = $derived(Math.floor(Math.max(0, deck.position_ms) / 1000));
	const liveBpm = $derived(
		playbackBpm({
			beats: deck.anlz?.beatgrid.beats,
			positionSec: positionBucketSec,
			tempoRatio: deck.pitch,
			tagBpm: deck.bpm
		})
	);
	// DECK-KEY-01: the deck's resolved key (else its "?" guess), as the deck header reads it.
	const liveKeyAt = $derived(
		deckKeyAtPlayheadNow({
			anlz: deck.anlz,
			position_ms: positionBucketSec * 1000,
			key_shift_semitones: deck.key_shift_semitones,
			key: deck.key,
			key_guess: deck.key_guess ?? null
		})
	);
	const liveKey = $derived(liveKeyAt.display);
	const liveKeyLabel = $derived(
		liveKey === null ? null : liveKeyAt.isGuess && liveKey !== '--' ? `${liveKey}?` : liveKey
	);
	const keyColor = $derived(camelotKeyColor(liveKey));
	const coverUrl = $derived.by(() => {
		const sid = deck.stable_id;
		if (artworkSrc === null || sid === null || !coverVisible(sid)) return null;
		return coverSrc(artworkSrc, sid);
	});

	function loadedStableId(): string {
		if (deck.stable_id === null) throw new Error('artwork event for an empty deck');
		return deck.stable_id;
	}
</script>

<WaveTrackSummaryView
	{fullTitle}
	artworkSrc={coverUrl}
	liveKey={liveKeyLabel}
	{keyColor}
	{liveBpm}
	deckNumber={deck.deck_id}
	deckNumberArt={uiPrefs.deck_number_art}
	onArtworkLoad={() => coverLoaded(loadedStableId())}
	onArtworkError={() => coverFailed(loadedStableId())}
/>
