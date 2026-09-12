<script lang="ts">
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		createPlaylistSet,
		createPlaylistSetRun,
		listPlaylistSets,
		type PlaylistSet
	} from '$lib/rb/api-playlist-sets';
	import { setTabLabel } from './playlist-set-tabs';

	let { playlistId }: { playlistId: string } = $props();

	let sets: PlaylistSet[] = $state([]);
	let selectedId = $state<number | null>(null);
	let mode: 'practice' | 'performance' = $state('practice');
	let newName = $state('');
	let creating = $state(false);
	let running = $state(false);

	async function refresh(): Promise<void> {
		sets = await listPlaylistSets(playlistId);
		if (selectedId !== null && !sets.some((s) => s.id === selectedId)) {
			selectedId = null;
		}
	}

	async function handleCreate(): Promise<void> {
		const name = newName.trim();
		if (!name || creating) return;
		creating = true;
		try {
			const created = await createPlaylistSet(playlistId, name);
			newName = '';
			await refresh();
			selectedId = created.id;
		} finally {
			creating = false;
		}
	}

	async function handleRun(kind: 'practice' | 'performance'): Promise<void> {
		if (selectedId === null || running) return;
		running = true;
		try {
			await createPlaylistSetRun(playlistId, selectedId, kind);
			await refresh();
		} finally {
			running = false;
		}
	}

	$effect(() => {
		const id = playlistId;
		selectedId = null;
		const unkind = subscribeKind('playlist_sets', (ids) => {
			if (ids.includes(id)) {
				void refresh();
			}
		});
		const unresync = subscribeResync(() => {
			void refresh();
		});
		void refresh();
		return () => {
			unkind();
			unresync();
		};
	});
</script>

<div class="playlist-set-tabs" data-testid="playlist-set-tabs">
	<div class="tab-row">
		{#each sets as set (set.id)}
			<button
				type="button"
				class="set-tab"
				class:selected={selectedId === set.id}
				data-testid="playlist-set-tab"
				data-set-id={set.id}
				aria-selected={selectedId === set.id}
				onclick={() => {
					selectedId = set.id;
				}}
			>
				{setTabLabel(set.name, set.play_count)}
			</button>
		{/each}
		<div class="create-set">
			<input
				type="text"
				placeholder="Set name"
				bind:value={newName}
				data-testid="playlist-set-create-input"
			/>
			<button
				type="button"
				data-testid="playlist-set-create"
				disabled={creating || !newName.trim()}
				onclick={() => void handleCreate()}
			>
				+ Set
			</button>
		</div>
	</div>
	{#if selectedId !== null}
		<div class="mode-row">
			<button
				type="button"
				data-testid="set-mode-practice"
				aria-pressed={mode === 'practice'}
				class:pressed={mode === 'practice'}
				onclick={() => {
					mode = 'practice';
				}}
			>
				Practice
			</button>
			<button
				type="button"
				data-testid="set-mode-perform"
				aria-pressed={mode === 'performance'}
				class:pressed={mode === 'performance'}
				onclick={() => {
					mode = 'performance';
				}}
			>
				Perform
			</button>
			<button
				type="button"
				data-testid="set-run-practice"
				disabled={running}
				onclick={() => void handleRun('practice')}
			>
				Start practice
			</button>
			<button
				type="button"
				data-testid="set-run-perform"
				disabled={running}
				onclick={() => void handleRun('performance')}
			>
				Start performance
			</button>
		</div>
	{/if}
</div>

<style>
	.playlist-set-tabs {
		flex: none;
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-panel);
	}
	.tab-row {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		padding: 4px 6px;
		align-items: center;
	}
	.set-tab {
		border: 1px solid var(--rb-border);
		background: color-mix(in srgb, var(--rb-panel) 85%, var(--rb-accent) 15%);
		color: var(--rb-text);
		border-radius: 4px;
		padding: 2px 8px;
		font-size: var(--rb-fs-label);
		cursor: pointer;
		opacity: 0;
		transform: translateY(-4px);
		animation: set-tab-in 150ms ease forwards;
	}
	.set-tab.selected {
		border-color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-panel) 70%, var(--rb-accent) 30%);
	}
	.create-set {
		display: flex;
		gap: 4px;
		margin-left: auto;
	}
	.create-set input {
		width: 8rem;
		font-size: var(--rb-fs-label);
	}
	.mode-row {
		display: flex;
		gap: 6px;
		padding: 4px 6px 6px;
		border-top: 1px solid color-mix(in srgb, var(--rb-border) 70%, transparent);
	}
	.mode-row button {
		font-size: var(--rb-fs-label);
	}
	.mode-row button.pressed {
		border-color: var(--rb-accent);
	}
	@keyframes set-tab-in {
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
