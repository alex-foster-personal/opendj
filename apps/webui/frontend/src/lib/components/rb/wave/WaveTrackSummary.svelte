<script lang="ts">
	// Compact track identity in a waveform gutter. Artwork failures and empty
	// decks have named slates so either state cannot be mistaken for a blank.
	import { artworkUrl } from '$lib/rb/api-rb';
	import type { DeckState } from '$lib/rb/deck-state-types';

	const { deck }: { deck: DeckState } = $props();
	const trackName = $derived.by(() => {
		if (deck.stable_id === null) return 'No track loaded';
		if (deck.title === null) throw new Error(`loaded deck ${deck.stable_id} is missing a title`);
		return deck.title;
	});
	const artworkSrc = $derived(deck.stable_id === null ? null : artworkUrl(deck.stable_id, 's'));
	let artworkFailed = $state(false);
	let artworkLoaded = $state(false);
	let trackNameEl = $state<HTMLSpanElement | null>(null);
	let trackNameScrubPx = $state(0);
	let trackNameScrubbing = $state(false);

	$effect(() => {
		void artworkSrc;
		artworkFailed = false;
		artworkLoaded = false;
	});

	function startTrackNameScrub(): void {
		if (trackNameEl === null) throw new Error('track-name element was not mounted');
		trackNameScrubPx = Math.max(0, trackNameEl.scrollWidth - trackNameEl.clientWidth);
		trackNameScrubbing = trackNameScrubPx > 0;
	}

	function stopTrackNameScrub(): void {
		trackNameScrubbing = false;
	}
</script>

<div class="wave-track-summary">
	{#if artworkSrc !== null && !artworkFailed}
		<span class="wave-art" title={artworkLoaded ? 'Track artwork' : 'Loading artwork'}>
			<img
				class:loaded={artworkLoaded}
				src={artworkSrc}
				alt=""
				onload={() => {
					artworkLoaded = true;
				}}
				onerror={() => {
					artworkFailed = true;
				}}
			/>
			<span class:visible={!artworkLoaded} class="wave-art-slate" aria-hidden="true">ART</span>
		</span>
	{:else if deck.stable_id === null}
		<span class="wave-art-slate visible" title="No track loaded">EMPTY</span>
	{:else}
		<span class="wave-art-slate visible" title="Artwork unavailable - image could not be loaded">NO ART</span>
	{/if}
	<span
		bind:this={trackNameEl}
		class="wave-track-name"
		class:scrubbing={trackNameScrubbing}
		style:--track-name-scrub-px={`${trackNameScrubPx}px`}
		style:animation-duration={`${Math.max(trackNameScrubPx / 30, 0.6)}s`}
		title={trackName}
		role="note"
		onpointerenter={startTrackNameScrub}
		onpointerleave={stopTrackNameScrub}
	>
		<span>{trackName}</span>
	</span>
</div>

<style>
	.wave-track-summary {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
	}
	.wave-art,
	.wave-art-slate {
		position: relative;
		display: block;
		width: 27px;
		height: 25px;
		flex: none;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		box-sizing: border-box;
	}
	.wave-art img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		opacity: 0;
	}
	.wave-art img.loaded {
		opacity: 1;
	}
	.wave-art-slate {
		display: none;
		place-items: center;
		color: var(--rb-text-dim);
		font-size: 7px;
		font-weight: 700;
		letter-spacing: 0.04em;
	}
	.wave-art-slate.visible {
		display: grid;
	}
	.wave-art .wave-art-slate {
		position: absolute;
		inset: 0;
		width: auto;
		height: auto;
	}
	.wave-track-name {
		display: block;
		width: 100%;
		overflow: hidden;
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 12px;
		white-space: nowrap;
	}
	.wave-track-name > span {
		display: inline-block;
		min-width: 100%;
		white-space: nowrap;
	}
	.wave-track-name.scrubbing > span {
		animation-name: track-name-scrub;
		animation-timing-function: linear;
		animation-iteration-count: infinite;
		animation-direction: alternate;
	}
	@keyframes track-name-scrub {
		from {
			transform: translateX(0);
		}
		to {
			transform: translateX(calc(-1 * var(--track-name-scrub-px)));
		}
	}
</style>
