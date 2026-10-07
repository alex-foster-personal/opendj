<script lang="ts">
	// LIBWS-01: Library mode's Crate Desk. The same browser Gig mounts under
	// its decks gets the whole window here, with a preview bar above it and a
	// track inspector beside it. Loaded lazily by routes/+page.svelte so a cold
	// open of "/" (which PERFMODE-11 sends on to Gig) does not download it.
	// Palette is scoped under .perf-root (theme.css); never touch :root.
	import '$lib/rb/theme.css';
	import BrowserPanel from '$lib/components/rb/BrowserPanel.svelte';
	import LibraryPreviewBar from './LibraryPreviewBar.svelte';
	import LibraryTopBar from './LibraryTopBar.svelte';
	import TrackInspector from './TrackInspector.svelte';
	import {
		LIBRARY_DECK_LOAD_REFUSAL,
		publishLibrarySelection
	} from '$lib/library/library-workspace.svelte';

	// LIBWS-01/03: the browser learns it is in Library mode from this prop, from
	// its first render: it refuses deck loads and publishes its selection here.
	const library = { refusal: LIBRARY_DECK_LOAD_REFUSAL, publish: publishLibrarySelection };
</script>

<div class="perf-root library-root" data-testid="library-workspace">
	<LibraryTopBar />
	<LibraryPreviewBar />
	<div class="desk">
		<BrowserPanel {library} />
		<TrackInspector />
	</div>
</div>

<style>
	.library-root {
		position: fixed;
		inset: 0;
		display: grid;
		grid-template-areas:
			'topbar'
			'preview'
			'desk';
		grid-template-rows: var(--rb-topbar-h) 48px minmax(0, 1fr);
		background: var(--rb-bg);
	}
	.desk {
		grid-area: desk;
		display: grid;
		grid-template-areas: 'browser inspector';
		grid-template-columns: minmax(0, 1fr) 300px;
		min-height: 0;
	}
</style>
