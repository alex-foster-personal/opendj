<script lang="ts">
	import VirtualList from './VirtualList.svelte';
	import { TreeAutolists } from './tree-autolists.svelte';
	import type { AutolistGroupId, AutolistSelection } from '$lib/smartlists/autolist-rule';

	let {
		onselectionchange
	}: {
		onselectionchange: (selection: AutolistSelection, title: string) => void;
	} = $props();

	const autolists = new TreeAutolists((sel, title) => onselectionchange(sel, title));

	const GROUPS: { id: AutolistGroupId; title: string; testid: string }[] = [
		{ id: 'genre', title: 'Genre', testid: 'autolist-group-genre' },
		{ id: 'rating', title: 'Rating', testid: 'autolist-group-rating' },
		{ id: 'bpm', title: 'BPM', testid: 'autolist-group-bpm' }
	];

	$effect(() => {
		autolists.startIndexWorker();
		return () => autolists.destroy();
	});
</script>

<div class="autolist-root">
	{#each GROUPS as group (group.id)}
		<div class="group" data-testid={group.testid}>
			<div
				class="row header"
				role="button"
				tabindex="0"
				onclick={() => autolists.toggleGroup(group.id)}
				onkeydown={(e) => {
					if (e.key === 'Enter') autolists.toggleGroup(group.id);
				}}
			>
				<span class="disclosure" class:open={autolists.openGroups[group.id]}>&#9656;</span>
				<span class="name">{group.title}</span>
			</div>
			{#if autolists.openGroups[group.id]}
				{@const buckets = autolists.bucketsFor(group.id)}
				{#if group.id === 'genre' && buckets.length > 40}
					<VirtualList itemCount={buckets.length} rowHeight={20}>
						{#snippet children(start, end)}
							{#each buckets.slice(start, end) as bucket (bucket.id)}
								<div
									class="row child"
									class:selected={autolists.isSelected(group.id, bucket.id)}
									role="button"
									tabindex="0"
									onclick={() => autolists.toggleChild(group.id, bucket.id)}
									onkeydown={(e) => {
										if (e.key === 'Enter') autolists.toggleChild(group.id, bucket.id);
									}}
								>
									<span class="name">{bucket.label}</span>
									{#if bucket.count !== undefined}
										<span class="count" title="track count">{bucket.count}</span>
									{/if}
								</div>
							{/each}
						{/snippet}
					</VirtualList>
				{:else}
					{#each buckets as bucket (bucket.id)}
						<div
							class="row child"
							class:selected={autolists.isSelected(group.id, bucket.id)}
							role="button"
							tabindex="0"
							onclick={() => autolists.toggleChild(group.id, bucket.id)}
							onkeydown={(e) => {
								if (e.key === 'Enter') autolists.toggleChild(group.id, bucket.id);
							}}
						>
							<span class="name">{bucket.label}</span>
							{#if bucket.count !== undefined}
								<span class="count" title="track count">{bucket.count}</span>
							{/if}
						</div>
					{/each}
				{/if}
			{/if}
		</div>
	{/each}
	{#if autolists.error !== null}
		<div class="row rb-inert" title={autolists.error}>Autolist load failed</div>
	{/if}
</div>

<style>
	.autolist-root {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px 0 22px;
		color: var(--rb-text);
		cursor: pointer;
		white-space: nowrap;
	}
	.row.header {
		padding-left: 6px;
		font-weight: 500;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row.selected {
		background: var(--rb-select);
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.count {
		flex: none;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
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
</style>
