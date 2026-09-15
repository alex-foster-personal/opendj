<script lang="ts">
	import { REVEAL_TRACK_TITLE, runRevealTracks } from '$lib/components/rb/browser/track-context-menu';

	let { stableId }: { stableId: string } = $props();

	// Open in Rekordbox / djay have no backend yet (UXR-02): inert, never a dead click.
	const INERT_TITLE = 'not implemented - see PARITY-TODO';
	const INERT_ACTIONS = ['Open in Rekordbox', 'Open in djay'];
</script>

<p class="track-actions">
	<button type="button" title={REVEAL_TRACK_TITLE} onclick={() => void runRevealTracks([stableId])}>
		Show in Finder
	</button>
	{#each INERT_ACTIONS as label}
		<button type="button" disabled title={INERT_TITLE} aria-label={`${label} - ${INERT_TITLE}`}>{label}</button>
	{/each}
</p>
