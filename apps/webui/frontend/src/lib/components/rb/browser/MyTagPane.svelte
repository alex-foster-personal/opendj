<script lang="ts">
	// Issue #10026: the My Tag pane docked right of the track table (rekordbox
	// My Tag pane, mimicked). Stateless: the catalog grouped by tag type as
	// checkboxes bound to the selection; BrowserPanel owns the write. Space on a
	// focused checkbox toggles it (native), and the pane follows the table in
	// the tab order because it sits after it in the DOM.
	import type { MyTagPaneGroup, MyTagPaneTag } from './mytag-pane-state';

	let {
		groups,
		selectedCount,
		busy,
		ontoggle
	}: {
		groups: MyTagPaneGroup[];
		/** Tracks in the active pane's selection. */
		selectedCount: number;
		/** A write is in flight; the boxes wait for it. */
		busy: boolean;
		ontoggle: (tag: MyTagPaneTag) => void;
	} = $props();
</script>

<aside class="mytag-pane" data-testid="mytag-pane" aria-label="My Tag">
	<div class="mytag-header">
		<span class="mytag-mark">My Tag</span>
		{#if selectedCount > 1}
			<span class="mytag-count">{selectedCount} tracks</span>
		{/if}
	</div>
	{#if selectedCount === 0}
		<p class="status" role="status" data-testid="mytag-pane-empty">
			Select a track to see and tick its tags.
		</p>
	{:else if groups.length === 0}
		<p class="status" role="status">No tags yet. Add one from the Tags cell or the tag editor.</p>
	{:else}
		<div class="mytag-groups">
			{#each groups as group (group.id)}
				<fieldset class="mytag-group" data-testid="mytag-group" data-group={group.id}>
					<legend>{group.label}</legend>
					{#each group.tags as tag (tag.name)}
						<label class="mytag-tag" title="{tag.name}: on {tag.held} of {selectedCount} selected">
							<input
								type="checkbox"
								data-testid="mytag-check"
								data-tag={tag.name}
								data-state={tag.state}
								checked={tag.state === 'checked'}
								indeterminate={tag.state === 'mixed'}
								disabled={busy}
								onchange={() => ontoggle(tag)}
							/>
							{#if tag.color !== null}
								<span class="swatch" style:background={tag.color} aria-hidden="true"></span>
							{/if}
							<span class="name">{tag.name}</span>
						</label>
					{/each}
				</fieldset>
			{/each}
		</div>
	{/if}
</aside>

<style>
	.mytag-pane {
		display: flex;
		flex: 0 0 180px;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
		border-left: 1px solid var(--rb-border);
		background: var(--rb-panel);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
	}
	.mytag-header {
		display: flex;
		align-items: center;
		gap: 6px;
		min-height: 22px;
		padding: 0 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.mytag-mark {
		font-weight: 600;
	}
	.mytag-count,
	.status {
		color: var(--rb-text-dim);
	}
	.status {
		margin: 0;
		padding: 6px;
	}
	.mytag-groups {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
	.mytag-group {
		margin: 0;
		padding: 2px 6px 4px;
		border: 0;
		border-bottom: 1px solid var(--rb-border);
	}
	.mytag-group legend {
		padding: 2px 0;
		color: var(--rb-text-dim);
		font-weight: 600;
	}
	.mytag-tag {
		display: flex;
		align-items: center;
		gap: 4px;
		height: 20px;
		cursor: pointer;
	}
	.swatch {
		flex: 0 0 8px;
		width: 8px;
		height: 8px;
		border-radius: 2px;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
