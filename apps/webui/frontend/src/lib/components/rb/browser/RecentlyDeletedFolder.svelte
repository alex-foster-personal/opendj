<script lang="ts">
	import { onDestroy } from 'svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { TreeRecentlyDeleted } from './tree-recently-deleted.svelte';

	const recentlyDeleted = new TreeRecentlyDeleted();
	onDestroy(() => recentlyDeleted.destroy());

	function _countText(): string {
		if (recentlyDeleted.error !== null) return '!';
		if (recentlyDeleted.rows === null) return '...';
		return String(recentlyDeleted.rows.length);
	}

	function _countTitle(): string {
		if (recentlyDeleted.error !== null) {
			return `deleted playlists unavailable: ${recentlyDeleted.error}`;
		}
		if (recentlyDeleted.rows === null) return 'loading deleted playlists';
		return `${recentlyDeleted.rows.length} deleted playlists`;
	}

	async function restorePlaylist(id: string, name: string): Promise<void> {
		try {
			await recentlyDeleted.restore(id);
			pushToast(`Restored playlist "${name}"`, 'info');
		} catch (exc) {
			pushToast(`restore failed: ${String(exc)}`, 'error');
		}
	}
</script>

<div
	class="row folder"
	data-testid="playlist-recently-deleted"
	role="button"
	tabindex="0"
	onclick={() => recentlyDeleted.toggle()}
	onkeydown={(e) => {
		if (e.key === 'Enter') recentlyDeleted.toggle();
	}}
>
	<span class="disclosure" class:open={recentlyDeleted.open}>&#9656;</span>
	<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
		<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
	</svg>
	<span class="name">Recently deleted</span>
	<span class="count" title={_countTitle()}>{_countText()}</span>
</div>
{#if recentlyDeleted.open}
	{#if recentlyDeleted.error !== null}
		<div class="row child rb-inert" title={recentlyDeleted.error}>
			<span class="name error">deleted playlists unavailable</span>
		</div>
	{:else if recentlyDeleted.rows === null}
		<div class="row child rb-inert">
			<span class="name dim">...</span>
		</div>
	{:else if recentlyDeleted.rows.length === 0}
		<div class="row child rb-inert" data-testid="playlist-recently-deleted-empty">
			<span class="name dim">no deleted playlists</span>
		</div>
	{:else}
		{#each recentlyDeleted.rows as row (row.playlist_id)}
			<div class="row child rb-inert" data-testid="playlist-deleted-row">
				<span class="name" title={row.name}>{row.name}</span>
				<button
					type="button"
					class="restore"
					title="Restore playlist"
					data-testid="restore-playlist"
					onclick={(e) => {
						e.stopPropagation();
						void restorePlaylist(row.playlist_id, row.name);
					}}
				>
					&#8635;
				</button>
			</div>
		{/each}
	{/if}
{/if}

<style>
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
