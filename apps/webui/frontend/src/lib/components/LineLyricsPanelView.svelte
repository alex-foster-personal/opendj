<script lang="ts">
	// Props-only half of LineLyricsPanel: the container owns the fetch and
	// hands over its three states. The shape is the slice of TrackLyrics this
	// panel reads, so the view adds no importer to api-rb.ts.
	type LineLyrics = { source: string; lines: { start_ms: number; text: string }[] };

	let {
		lyrics,
		loading,
		error
	}: { lyrics: LineLyrics | null; loading: boolean; error: string | null } = $props();

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
