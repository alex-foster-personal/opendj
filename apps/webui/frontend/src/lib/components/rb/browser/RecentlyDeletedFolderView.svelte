<script lang="ts">
	// RecentlyDeletedFolder's rows as props only: the container owns the
	// deleted-playlist list and the restore call (null rows = still loading).
	import type { DeletedPlaylistOut } from '$lib/rb/playlist-deleted';

	let {
		open,
		rows,
		error,
		ontoggle,
		onrestore
	}: {
		open: boolean;
		rows: DeletedPlaylistOut[] | null;
		error: string | null;
		ontoggle: () => void;
		onrestore: (id: string, name: string) => void;
	} = $props();

	function _countText(): string {
		if (error !== null) return '!';
		if (rows === null) return '...';
		return String(rows.length);
	}

	function _countTitle(): string {
		if (error !== null) {
			return `deleted playlists unavailable: ${error}`;
		}
		if (rows === null) return 'loading deleted playlists';
		return `${rows.length} deleted playlists`;
	}
</script>

<!-- The read-only marker wraps the header AND its child rows: they render as
     siblings, so a marker on the header alone leaves every child unmarked. -->
<div class="subtree" data-menu-target="tree-readonly">
<div
	class="row folder"
	data-testid="playlist-recently-deleted"
	role="button"
	tabindex="0"
	onclick={() => ontoggle()}
	onkeydown={(e) => {
		if (e.key === 'Enter') ontoggle();
	}}
>
	<span class="disclosure" class:open>&#9656;</span>
	<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
		<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
	</svg>
	<span class="name">Recently deleted</span>
	<span class="count" title={_countTitle()}>{_countText()}</span>
</div>
{#if open}
	{#if error !== null}
		<div class="row child rb-inert" title={error}>
			<span class="name error">deleted playlists unavailable</span>
		</div>
	{:else if rows === null}
		<div class="row child rb-inert">
			<span class="name dim">...</span>
		</div>
	{:else if rows.length === 0}
		<div class="row child rb-inert" data-testid="playlist-recently-deleted-empty">
			<span class="name dim">no deleted playlists</span>
		</div>
	{:else}
		{#each rows as row (row.playlist_id)}
			<div class="row child rb-inert" data-testid="playlist-deleted-row">
				<span class="name" title={row.name}>{row.name}</span>
				<button
					type="button"
					class="restore"
					title="Restore playlist"
					data-testid="restore-playlist"
					onclick={(e) => {
						e.stopPropagation();
						onrestore(row.playlist_id, row.name);
					}}
				>
					&#8635;
				</button>
			</div>
		{/each}
	{/if}
{/if}
</div>

<style>
	.subtree {
		display: contents;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		cursor: pointer;
		white-space: nowrap;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.row.child {
		padding-left: 22px;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.disclosure {
		flex: none;
		display: inline-block;
		width: 8px;
		color: var(--rb-text-dim);
		transition: transform 0.1s;
	}
	.disclosure.open {
		transform: rotate(90deg);
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
	.name.dim,
	.name.error {
		color: var(--rb-text-dim);
	}
	.count {
		flex: none;
		min-width: 4ch;
		align-self: stretch;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
	}
	.restore {
		flex: none;
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		cursor: pointer;
		padding: 0 2px;
		line-height: 1;
	}
	.restore:hover {
		color: var(--rb-accent);
	}
</style>
