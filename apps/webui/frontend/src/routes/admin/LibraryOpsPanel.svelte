<script lang="ts">
	/** LIBM-132: library tidying entrypoints (no on-disk file deletion). */
</script>

<section class="panel">
	<h3>Library operations</h3>
	<p class="sub">
		Tidy the catalog without deleting audio files. Remove-from-library keeps files on disk.
	</p>
	<ul>
		<li><a href="/reconcile">Broken links / missing files</a></li>
		<li><a href="/dedup">Duplicate clusters</a></li>
		<li>
			<code>opendj api DELETE /api/v1/tracks/{'{stable_id}'}</code> - remove from library (soft delete)
		</li>
		<li><a href="/performance">Playlist hygiene</a> - tree menus for playlists and smartlists</li>
	</ul>
</section>

<style>
	.panel {
		margin-top: 1.5rem;
	}
	.sub {
		opacity: 0.85;
		font-size: 0.9rem;
	}
	ul {
		line-height: 1.6;
	}
</style>
