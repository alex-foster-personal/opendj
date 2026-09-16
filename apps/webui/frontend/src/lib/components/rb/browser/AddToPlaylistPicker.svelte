<script lang="ts">
	import { tick } from 'svelte';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import {
		filterPlaylistsByName,
		PLAYLIST_PICKER_SEARCH_THRESHOLD
	} from '$lib/rb/add-to-playlist';

	let {
		open = false,
		playlists = [] as PlaylistNode[],
		onpick,
		onclose
	}: {
		open: boolean;
		playlists: PlaylistNode[];
		onpick: (node: PlaylistNode) => void;
		onclose: () => void;
	} = $props();

	let query = $state('');
	let searchInput = $state<HTMLInputElement | null>(null);
	let dialogEl = $state<HTMLDivElement | null>(null);
	let playlistButtons = $state<HTMLButtonElement[]>([]);

	const showSearch = $derived(playlists.length > PLAYLIST_PICKER_SEARCH_THRESHOLD);
	const filtered = $derived(filterPlaylistsByName(playlists, query));

	$effect(() => {
		if (!open) {
			query = '';
			return;
		}
		void tick().then(() => {
			if (showSearch && searchInput !== null) {
				searchInput.focus();
			} else if (playlistButtons.length > 0) {
				playlistButtons[0]?.focus();
			} else {
				dialogEl?.focus();
			}
		});
	});

	function onWindowKeydown(e: KeyboardEvent): void {
		if (open && e.key === 'Escape') onclose();
	}

	/**
	 * The dialog stops keydown propagation so typing inside it never reaches
	 * the browser panel's global hotkeys. That also stopped Escape from ever
	 * reaching the window handler above, so the picker could only be closed by
	 * clicking the scrim: focus lands INSIDE the dialog when it opens, so every
	 * Escape bubbled into this element and died here. Close first, then stop.
	 */
	function onDialogKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') onclose();
		e.stopPropagation();
	}

	function onSearchKeydown(e: KeyboardEvent): void {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			playlistButtons[0]?.focus();
		}
	}

	function onPlaylistKeydown(e: KeyboardEvent, index: number): void {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			playlistButtons[index + 1]?.focus();
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			if (index === 0 && showSearch && searchInput !== null) {
				searchInput.focus();
			} else {
				playlistButtons[index - 1]?.focus();
			}
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if open}
	<!-- scrim click closes; Escape closes on the dialog itself before it stops propagation -->
	<div class="scrim" role="presentation" onclick={onclose} onkeydown={onWindowKeydown}>
		<div
			class="modal"
			bind:this={dialogEl}
			role="dialog"
			aria-modal="true"
			aria-label="Add to playlist"
			data-testid="add-to-playlist-picker"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={onDialogKeydown}
		>
			<div class="m-title">Add to playlist</div>

			{#if showSearch}
				<input
					bind:this={searchInput}
					class="m-search"
					type="search"
					aria-label="Filter playlists"
					autocomplete="off"
					spellcheck="false"
					bind:value={query}
					onkeydown={onSearchKeydown}
				/>
			{/if}

			<div class="m-list">
				{#if playlists.length === 0}
					<div class="m-empty">No writable playlists</div>
				{:else if filtered.length === 0}
					<div class="m-empty">No matching playlists</div>
				{:else}
					{#each filtered as node, index (node.playlist_id)}
						<button
							type="button"
							class="m-row"
							bind:this={playlistButtons[index]}
							onclick={() => onpick(node)}
							onkeydown={(e) => onPlaylistKeydown(e, index)}
						>
							<span class="m-name">{node.name}</span>
							<span class="m-count" title="Track count in this playlist">{node.track_count}</span>
						</button>
					{/each}
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 80;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.55);
	}
	.modal {
		width: min(420px, 92vw);
		max-height: 70vh;
		overflow-y: auto;
		padding: 14px 16px;
		background: var(--rb-panel-raised, #14171b);
		border: 1px solid var(--rb-border, #2a2f36);
		border-radius: 6px;
		color: var(--rb-text, #c8cfd6);
		font-size: 12px;
	}
	.m-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--rb-text, #e8edf2);
		margin-bottom: 10px;
	}
	.m-search {
		width: 100%;
		box-sizing: border-box;
		padding: 6px 8px;
		margin-bottom: 10px;
		background: var(--rb-select, #0d0f12);
		border: 1px solid var(--rb-border, #2a2f36);
		border-radius: 3px;
		color: var(--rb-text, #e8edf2);
		font-size: 12px;
	}
	.m-list {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.m-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		border: 1px solid transparent;
		border-radius: 4px;
		background: transparent;
		color: inherit;
		font-size: 12px;
		text-align: left;
		cursor: pointer;
	}
	.m-row:hover,
	.m-row:focus-visible {
		background: var(--rb-select, #1a1f26);
		border-color: var(--rb-border, #2a2f36);
		outline: none;
	}
	.m-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.m-count {
		flex-shrink: 0;
		opacity: 0.75;
		font-variant-numeric: tabular-nums;
	}
	.m-empty {
		padding: 12px 8px;
		opacity: 0.75;
		text-align: center;
	}
</style>
