<script lang="ts">
	// PlaylistSetTabs as props only: the container loads the playlist's sets
	// and runs create and start; selection and the new-set name are bound
	// back to it because its refresh and create flows read and reset them.
	import type { PlaylistSet } from '$lib/rb/api-playlist-sets';
	import { setTabLabel } from './playlist-set-tabs';

	let {
		sets,
		selectedId = $bindable(null),
		mode = $bindable('practice'),
		newName = $bindable(''),
		creating,
		running,
		runNote = null,
		runError = false,
		runIsLive = () => false,
		startLabel = (kind) => (kind === 'practice' ? 'Start practice' : 'Start performance'),
		oncreate,
		onrun
	}: {
		sets: PlaylistSet[];
		selectedId?: number | null;
		mode?: 'practice' | 'performance';
		newName?: string;
		creating: boolean;
		running: boolean;
		runNote?: string | null;
		runError?: boolean;
		runIsLive?: (kind: 'practice' | 'performance') => boolean;
		startLabel?: (kind: 'practice' | 'performance') => string;
		oncreate: () => void;
		onrun: (kind: 'practice' | 'performance') => void;
	} = $props();
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
				onclick={() => oncreate()}
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
				aria-pressed={runIsLive('practice')}
				class:live={runIsLive('practice')}
				onclick={() => onrun('practice')}
			>
				{startLabel('practice')}
			</button>
			<button
				type="button"
				data-testid="set-run-perform"
				disabled={running}
				aria-pressed={runIsLive('performance')}
				class:live={runIsLive('performance')}
				onclick={() => onrun('performance')}
			>
				{startLabel('performance')}
			</button>
			{#if runNote !== null}
				<p
					class="run-status"
					class:error={runError}
					role="status"
					data-testid="set-run-status"
					title="Run status for this set. The play count is how many practice or performance runs this set has been started for."
				>
					{runNote}
				</p>
			{/if}
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
	.mode-row button.pressed,
	.mode-row button.live {
		border-color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-accent) 35%, var(--rb-panel));
		color: var(--rb-text);
	}
	.run-status {
		margin: 0 0 0 auto;
		align-self: center;
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
	}
	.run-status.error {
		color: var(--rb-red, #e55);
	}
	@keyframes set-tab-in {
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
