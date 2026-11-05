<script lang="ts">
	// PERFMODE-15 / TRACKIFY-UI-01: Trackify's listening view. One deck, a
	// position bar and no waveform painting, drawn in the same .perf-root
	// palette as Gig and the Library Crate Desk rather than unstyled defaults.
	// Palette is scoped under .perf-root (theme.css); never touch :root.
	import '$lib/rb/theme.css';
	import PositionBar from '$lib/components/rb/PositionBar.svelte';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { coverFailed, coverLoaded, coverSrc, coverVisible } from '$lib/rb/artwork-cover.svelte';
	import { deckAudioClockPositionMs, deckStates } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { requestTrackifySkipNext } from '$lib/rb/trackify-autoplay.svelte';
	import { TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';
	import { formatMsAsClock } from '$lib/rb/unexpected-pause';

	const deck = $derived(deckStates[TRACKIFY_DECK_ID]);
	const positionMs = $derived(deckAudioClockPositionMs(TRACKIFY_DECK_ID));
	const loaded = $derived(deck.stable_id !== null);
	const playTitle = $derived(
		!loaded ? 'no track loaded - nothing to play' : deck.playing ? 'Pause' : 'Play'
	);

	// The artwork route 404s when a track has none; the slate stays, never a
	// fabricated cover; the shared cover retry (LIBM-185) tries again later.

	async function togglePlay(): Promise<void> {
		await runPerformanceCommandFromUi({
			type: 'play',
			deck: TRACKIFY_DECK_ID,
			playing: !deck.playing
		});
	}

	function skipNext(): void {
		requestTrackifySkipNext();
	}
</script>

<section class="perf-root trackify-player" data-testid="trackify-player">
	<div class="cover" aria-hidden="true">
		{#if deck.stable_id !== null && coverVisible(deck.stable_id)}
			{@const sid = deck.stable_id}
			<img
				src={coverSrc(artworkUrl(sid, 'm'), sid)}
				alt=""
				data-expected-resource-error
				onload={() => coverLoaded(sid)}
				onerror={() => coverFailed(sid)}
				data-testid="trackify-artwork"
			/>
		{:else}
			<div class="cover-slate" data-testid="trackify-artwork-slate"></div>
		{/if}
	</div>

	<div class="now-playing">
		<p class="eyebrow">{deck.playing ? 'Now playing' : loaded ? 'Paused' : 'Trackify'}</p>
		<h1 title="Now playing track title">{deck.title ?? 'No track loaded'}</h1>
		<p class="artist" title="Now playing artist">
			{deck.artist ?? (loaded ? 'Unknown artist' : 'Autoplay picks the next track from your last playlist')}
		</p>
		{#if loaded}
			<p class="chips">
				{#if deck.bpm !== null}
					<span class="chip" title="Track tempo in beats per minute, from the library">{deck.bpm.toFixed(1)} BPM</span>
				{/if}
				{#if deck.key !== null}
					<span class="chip" title="Track key in Camelot notation, from the library">{deck.key}</span>
				{/if}
			</p>
		{/if}
	</div>

	<div class="progress">
		<PositionBar positionMs={positionMs} durationMs={deck.duration_ms} />
		<p class="times">
			<span title="Playback position (minutes:seconds)">{formatMsAsClock(positionMs)}</span>
			<span title="Track duration (minutes:seconds)">
				{deck.duration_ms !== null ? formatMsAsClock(deck.duration_ms) : '-:--'}
			</span>
		</p>
	</div>

	<div class="transport">
		<button type="button" class="play" onclick={() => void togglePlay()} disabled={!loaded} title={playTitle}>
			{deck.playing ? 'Pause' : 'Play'}
		</button>
		<button type="button" class="skip" onclick={skipNext} title="Skip to next track">Skip</button>
	</div>
</section>

<style>
	.trackify-player {
		box-sizing: border-box;
		min-height: 100%;
		display: grid;
		grid-template-columns: minmax(10rem, 18rem) minmax(0, 32rem);
		grid-template-areas:
			'cover info'
			'cover progress'
			'cover transport';
		align-content: center;
		justify-content: center;
		column-gap: 2rem;
		row-gap: 1.25rem;
		padding: 2rem 1.5rem;
		background: var(--rb-bg);
		color: var(--rb-text);
		font-family: var(--rb-font);
	}
	@media (max-width: 640px) {
		.trackify-player {
			grid-template-columns: minmax(0, 1fr);
			grid-template-areas: 'cover' 'info' 'progress' 'transport';
		}
		.cover {
			max-width: 14rem;
			justify-self: center;
		}
	}
	.cover {
		grid-area: cover;
		aspect-ratio: 1;
		width: 100%;
		border-radius: 6px;
		overflow: hidden;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
	}
	.cover img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.cover-slate {
		width: 100%;
		height: 100%;
		background: var(--rb-panel-raised);
	}
	.now-playing {
		grid-area: info;
		align-self: end;
		min-width: 0;
	}
	.eyebrow {
		margin: 0 0 0.35rem;
		font-size: var(--rb-fs-label);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--rb-text-dim);
	}
	.now-playing h1 {
		margin: 0;
		font-size: 1.6rem;
		line-height: 1.2;
		overflow-wrap: anywhere;
	}
	.artist {
		margin: 0.35rem 0 0;
		color: var(--rb-text-dim);
	}
	.chips {
		display: flex;
		gap: 0.4rem;
		margin: 0.75rem 0 0;
	}
	.chip {
		padding: 0.15rem 0.5rem;
		border-radius: 3px;
		font-size: var(--rb-fs-label);
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		font-variant-numeric: tabular-nums;
	}
	.progress {
		grid-area: progress;
		--text: var(--rb-text);
		--accent: var(--rb-accent);
	}
	.times {
		display: flex;
		justify-content: space-between;
		margin: 0.4rem 0 0;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.transport {
		grid-area: transport;
		display: flex;
		gap: 0.75rem;
	}
	.transport button {
		min-width: 6rem;
		padding: 0.5rem 1rem;
		border-radius: 4px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	.transport button.play {
		background: var(--rb-accent);
		border-color: var(--rb-accent);
		color: var(--rb-on-accent);
	}
	.transport button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.transport button:focus-visible {
		outline: 2px solid var(--rb-accent-glow);
		outline-offset: 2px;
	}
</style>
