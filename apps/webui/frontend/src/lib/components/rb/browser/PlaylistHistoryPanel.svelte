<script lang="ts">
	import { onMount } from 'svelte';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		fetchPlaylistHistory,
		redoPlaylistEdit,
		undoPlaylistEdit,
		type PlaylistHistoryEntry
	} from '$lib/rb/playlist-history';
	import {
		registerPlaylistHistoryAdapter,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';

	let cursor = $state(0);
	let canUndo = $state(false);
	let canRedo = $state(false);
	let entries: PlaylistHistoryEntry[] = $state([]);

	async function refresh(): Promise<void> {
		const hist = await fetchPlaylistHistory();
		cursor = hist.cursor;
		canUndo = hist.can_undo;
		canRedo = hist.can_redo;
		entries = hist.entries;
	}

	onMount(() => {
		const unregister = registerPlaylistHistoryAdapter({
			undo: async () => {
				await undoPlaylistEdit();
			},
			redo: async () => {
				await redoPlaylistEdit();
			}
		});
		const unkind = subscribeKind('playlists', () => {
			void refresh();
		});
		const unresync = subscribeResync(() => {
			void refresh();
		});
		void refresh();
		return () => {
			unregister();
			unkind();
			unresync();
		};
	});
</script>

<div class="history-toolbar">
	<div class="history-actions">
		<button
			type="button"
			class="hist-btn"
			data-testid="playlist-undo"
			title="Undo playlist edit (Ctrl/Cmd+Z)"
			disabled={!canUndo}
			onclick={() => void runPerformanceCommandFromUi({ type: 'playlist_undo' })}
		>
			Undo
		</button>
		<button
			type="button"
			class="hist-btn"
			data-testid="playlist-redo"
			title="Redo playlist edit (Ctrl/Cmd+Shift+Z)"
			disabled={!canRedo}
			onclick={() => void runPerformanceCommandFromUi({ type: 'playlist_redo' })}
		>
			Redo
		</button>
	</div>
	<ol class="history-list" data-testid="playlist-history-list">
		{#each entries as entry, index (entry.command_id)}
			<li
				class="history-item"
				class:pending={index >= cursor}
				aria-current={index === cursor - 1 ? 'step' : undefined}
			>
				{entry.label}
			</li>
		{/each}
	</ol>
</div>

<style>
	.history-toolbar {
		flex: none;
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-panel);
		padding: 4px 6px 2px;
	}
	.history-actions {
		display: flex;
		gap: 4px;
	}
	.hist-btn {
		flex: 1;
		padding: 2px 0;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.hist-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.hist-btn:not(:disabled):hover {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.history-list {
		margin: 4px 0 0;
		padding: 0;
		max-height: 72px;
		overflow-y: auto;
		list-style: none;
	}
	.history-item {
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
		padding: 1px 2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.history-item.pending {
		color: var(--rb-text-dim);
	}
	.history-item[aria-current='step'] {
		color: var(--rb-accent);
	}
	/* PERF-UI-01: the 72px history list plus Undo/Redo would crush
	 * tree-scroll at 1280x720 so playlist-all-tracks leaves the
	 * viewport. Hide the list; the buttons stay. */
	@media (max-height: 799px) {
		.history-list {
			display: none;
		}
	}
</style>
