<script lang="ts">
	import PositionBar from '$lib/components/rb/PositionBar.svelte';
	import { deckAudioClockPositionMs, deckStates } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { requestTrackifySkipNext } from '$lib/rb/trackify-autoplay.svelte';
	import { TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';

	const deck = $derived(deckStates[TRACKIFY_DECK_ID]);
	const positionMs = $derived(deckAudioClockPositionMs(TRACKIFY_DECK_ID));

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

<section class="trackify-player" data-testid="trackify-player">
	<div class="now-playing">
		<h1 title="Now playing track title">{deck.title ?? 'No track loaded'}</h1>
		<p class="artist" title="Now playing artist">{deck.artist ?? '—'}</p>
	</div>

	<PositionBar positionMs={positionMs} durationMs={deck.duration_ms} />

	<div class="transport">
		<button type="button" onclick={() => void togglePlay()} title={deck.playing ? 'Pause' : 'Play'}>
			{deck.playing ? 'Pause' : 'Play'}
		</button>
		<button type="button" onclick={skipNext} title="Skip to next track">Skip</button>
	</div>

	<p class="position-readout" title="Playback position in milliseconds">
		{Math.round(positionMs)} ms
		{#if deck.duration_ms !== null}
			/ <span title="Track duration in milliseconds">{Math.round(deck.duration_ms)}</span> ms
		{/if}
	</p>
</section>

<style>
	.trackify-player {
		display: grid;
		gap: 1rem;
		padding: 1.5rem;
		max-width: 40rem;
	}
	.now-playing h1 {
		margin: 0;
		font-size: 1.4rem;
	}
	.artist {
		margin: 0.25rem 0 0;
		opacity: 0.8;
	}
	.transport {
		display: flex;
		gap: 0.75rem;
	}
	.position-readout {
		font-size: 0.85rem;
		opacity: 0.75;
	}
</style>
