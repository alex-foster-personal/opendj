<script lang="ts">
	// Build unit: browser (COMPONENT-MAP 1.5). Stub - owner replaces the body.
	// Layout: far-left icon rail (inert) | playlist tree (real, /playlists +
	// /health counts - OUR numbers, never the screenshot's) | track list
	// (hydrated TrackRow[], editable star ratings via PATCH + If-Match,
	// client-side search + sort, dblclick/drag row -> deck load).
</script>

<section class="rb-browser rb-panel">
	<span>BrowserPanel stub - build unit: browser</span>
</section>

<style>
	.rb-browser {
		grid-area: browser;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--rb-text-dim);
		min-height: 0;
	}
</style>
