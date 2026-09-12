<script lang="ts">
	import { setAppPosture, uiPrefs } from '$lib/rb/prefs.svelte';
</script>

<div class="posture-chip" role="group" aria-label="Resource posture">
	<button
		type="button"
		class="posture-btn"
		aria-pressed={uiPrefs.app_posture === 'prep'}
		title="Resource posture: Prep. Background poll, workers, and prefetch stay at practice levels. Switch to Gig to cap them for a live set. This is not the app-mode chooser."
		onclick={() => setAppPosture('prep')}
	>
		Prep
	</button>
	<button
		type="button"
		class="posture-btn"
		aria-pressed={uiPrefs.app_posture === 'gig'}
		title="Resource posture: Gig. Library poll is 5 minutes, background workers are halved, prefetch is 2 tracks / 24 MiB. This is not the app-mode chooser."
		onclick={() => setAppPosture('gig')}
	>
		Gig
	</button>
</div>

<style>
	.posture-chip {
		display: inline-flex;
		gap: 2px;
		margin-left: 6px;
	}
	.posture-btn {
		font: inherit;
		font-size: 10px;
		line-height: 1;
		padding: 3px 6px;
		border: 1px solid var(--rb-border, #444);
		background: transparent;
		color: var(--rb-text-muted, #aaa);
		cursor: pointer;
	}
	.posture-btn[aria-pressed='true'] {
		border-color: var(--rb-accent, #4af);
		color: var(--rb-text, #fff);
	}
</style>
