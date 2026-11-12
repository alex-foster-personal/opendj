<script lang="ts">
	// Props-only half of WaveTrackSummary (DECKUX-40): artwork the full row
	// height, then the title over the live key and BPM. WaveTrackSummary.svelte
	// resolves the artwork URL, owns the shared cover retry calls and reads the
	// key and tempo at the playhead; this file owns only the artwork load state
	// and the hover scrub.
	import { gutterTrackTitle } from './track-title-display';
	import type { DeckNumberArt } from '$lib/rb/deck-number-art';
	import ArtworkDeckNumber from '../ArtworkDeckNumber.svelte';

	const {
		fullTitle,
		artworkSrc,
		liveKey,
		keyColor,
		liveBpm,
		onArtworkLoad,
		onArtworkError,
		deckNumber = null,
		deckNumberArt = 'off',
		deckStableId = null,
		deckPlaying = false
	}: {
		/** The loaded track's full title, shown whole on hover. */
		fullTitle: string;
		/** Deck artwork URL to show, or null when there is none to request. */
		artworkSrc: string | null;
		/** Key now, in Camelot notation, or null when unknown. */
		liveKey: string | null;
		/** Camelot wheel color for liveKey, or null for the dim default. */
		keyColor: string | null;
		/** Tempo now, in BPM, or null when unknown. */
		liveBpm: number | null;
		/** The artwork image loaded. */
		onArtworkLoad: () => void;
		/** The artwork image failed to load. */
		onArtworkError: () => void;
		/** ARTNUM-9649b: the deck number drawn over the loaded artwork, or null for none. */
		deckNumber?: number | null;
		/** ARTNUM-9649: the Deck number on artwork pref. */
		deckNumberArt?: DeckNumberArt;
		/** ARTNUM-11719: the loaded track's stable_id, the tint sampler's cache key. */
		deckStableId?: string | null;
		/** ARTNUM-11719: the deck plays (animated-cover resampling). */
		deckPlaying?: boolean;
	} = $props();

	const trackName = $derived(gutterTrackTitle(fullTitle));
	let artworkLoaded = $state(false);
	let artSizePx = $state(0);
	let trackNameEl = $state<HTMLSpanElement | null>(null);
	let trackNameScrubPx = $state(0);
	let trackNameScrubbing = $state(false);

	$effect(() => {
		void artworkSrc;
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
	{#if artworkSrc !== null}
		<span
			class="wave-art"
			title={artworkLoaded ? 'Track artwork' : 'Loading artwork'}
			bind:clientHeight={artSizePx}
		>
			<img
				class:loaded={artworkLoaded}
				src={artworkSrc}
				alt=""
				data-expected-resource-error="artwork-404"
				onload={() => {
					artworkLoaded = true;
					onArtworkLoad();
				}}
				onerror={() => {
					artworkLoaded = false;
					onArtworkError();
				}}
			/>
			{#if artworkLoaded && deckNumber !== null}
				<ArtworkDeckNumber
					deck={deckNumber}
					mode={deckNumberArt}
					sizePx={artSizePx}
					stableId={deckStableId}
					playing={deckPlaying}
				/>
			{/if}
		</span>
	{:else}
		<span
			class="wave-art"
			title="No artwork found: none in rekordbox, the file, a cover image in its folder, or online"
		></span>
	{/if}
	<div class="wave-text">
		<span
			bind:this={trackNameEl}
			class="wave-track-name"
			class:scrubbing={trackNameScrubbing}
			style:--track-name-scrub-px={`${trackNameScrubPx}px`}
			style:animation-duration={`${Math.max(trackNameScrubPx / 30, 0.6)}s`}
			title={fullTitle}
			role="note"
			onpointerenter={startTrackNameScrub}
			onpointerleave={stopTrackNameScrub}
		>
			<span>{trackName}</span>
		</span>
		<span class="wave-meta">
			{#if liveKey !== null}<span
					class="key"
					style:--wave-key-color={keyColor}
					title="Key now, in Camelot notation, after any key shift">{liveKey}</span
				>{/if}
			{#if liveBpm !== null}<span
					class="bpm"
					title="Tempo now, in BPM: the beatgrid tempo times the pitch fader"
					>{liveBpm.toFixed(1)}</span
				>{/if}
		</span>
	</div>
</div>

<style>
	.wave-track-summary {
		min-width: 0;
		flex: 1;
		display: flex;
		align-items: stretch;
		gap: 6px;
	}
	/* Full row height, square. No fill or border of its own: loaded artwork
	   covers it, and missing or loading artwork reads as empty space. */
	.wave-art {
		position: relative;
		display: block;
		flex: none;
		height: 100%;
		aspect-ratio: 1;
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
	/* Size containment lets the meta line drop out when the short-viewport
	   compact rows (18-24px) leave room for one line only. */
	.wave-text {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: 3px;
		container-type: size;
	}
	/* Fades the last 14px instead of cutting a letter in half; hover still
	   scrubs the full title through the space. */
	.wave-track-name {
		display: block;
		overflow: hidden;
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 12px;
		white-space: nowrap;
		-webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 14px), transparent);
		mask-image: linear-gradient(90deg, #000 calc(100% - 14px), transparent);
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
	.wave-meta {
		display: flex;
		align-items: baseline;
		gap: 6px;
		min-height: 11px;
		font-size: 9px;
		line-height: 11px;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	@container (max-height: 30px) {
		.wave-meta {
			display: none;
		}
	}
	.key {
		color: var(--wave-key-color, var(--rb-text-dim));
		font-weight: 600;
	}
	/* Gothic (and Light, Gothic flipped) de-colors chrome; its key reads as plain primary text. */
	:global(html:is([data-skin='mono-dev'], [data-skin='light'])) .key {
		color: var(--rb-text);
	}
	.bpm {
		color: var(--rb-text);
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}
</style>
