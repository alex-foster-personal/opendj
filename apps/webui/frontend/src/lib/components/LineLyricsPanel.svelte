<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchTrackLyrics, type TrackLyrics } from '$lib/rb/api-rb';

	let { stableId }: { stableId: string } = $props();

	let lyrics = $state<TrackLyrics | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	const headerLabel = $derived(
		lyrics === null
			? ''
			: lyrics.source === 'asr'
				? 'transcribed'
				: lyrics.source === 'lrclib'
					? 'LRCLIB'
					: lyrics.source
	);

	function formatMs(startMs: number): string {
		const totalSeconds = Math.floor(startMs / 1000);
		const minutes = Math.floor(totalSeconds / 60);
		const seconds = totalSeconds % 60;
		const millis = startMs % 1000;
		return `${minutes}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`;
	}

	onMount(() => {
		void (async () => {
			loading = true;
			error = null;
			try {
				lyrics = await fetchTrackLyrics(stableId);
			} catch (exc) {
				error = exc instanceof Error ? exc.message : String(exc);
			} finally {
				loading = false;
			}
		})();
	});
</script>

{#if loading}
	<p>Loading line lyrics...</p>
{:else if error}
	<p class="line-lyrics-error">{error}</p>
{:else if lyrics && lyrics.lines.length > 0}
	<section class="line-lyrics-panel">
		<h3 title="Line-synced lyrics source">{headerLabel}</h3>
		<ol class="line-lyrics-list">
			{#each lyrics.lines as line, index (index)}
				<li>
					<time datetime={`PT${line.start_ms / 1000}S`} title="Line start time in milliseconds"
						>{formatMs(line.start_ms)}</time
					>
					<span>{line.text}</span>
				</li>
			{/each}
		</ol>
	</section>
{/if}

<style>
	.line-lyrics-panel h3 {
		text-transform: capitalize;
	}
	.line-lyrics-list {
		list-style: none;
		padding: 0;
		margin: 0;
	}
	.line-lyrics-list li {
		display: grid;
		grid-template-columns: 6rem 1fr;
		gap: 0.75rem;
		padding: 0.25rem 0;
	}
	.line-lyrics-list time {
		font-variant-numeric: tabular-nums;
		opacity: 0.8;
	}
	.line-lyrics-error {
		color: var(--color-error, #c00);
	}
</style>
