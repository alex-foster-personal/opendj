<script lang="ts">
	// Presentational Missing Tracks tree row. Count is GET /reconcile/summary
	// total_broken (passed in); click selects the reserved 'missing' node.
	// LIBM-104: a system node, so its own icon and refused Rename / Delete.
	import SystemNodeActions from './SystemNodeActions.svelte';
	import { MISSING_TRACKS_ICON_PATH } from './system-tree-nodes';

	let {
		brokenCount,
		error,
		selected,
		onselect
	}: {
		brokenCount: number | null;
		error: string | null;
		selected: boolean;
		onselect: () => void;
	} = $props();

	function _countText(): string {
		if (error !== null) return '!';
		if (brokenCount === null) return '...';
		return String(brokenCount);
	}

	function _countTitle(): string {
		if (error !== null) return `broken count unavailable: ${error}`;
		if (brokenCount === null) return 'loading broken track count';
		return `${brokenCount} broken tracks`;
	}

	function _onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') onselect();
	}
</script>

<div
	data-menu-target="tree-readonly"
	class="row"
	class:selected
	data-testid="playlist-missing-tracks"
	data-system-node="missing"
	role="button"
	tabindex="0"
	onclick={onselect}
	onkeydown={_onKeydown}
>
	<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" data-testid="system-node-icon">
		<path d={MISSING_TRACKS_ICON_PATH} fill="currentColor" fill-rule="evenodd" />
	</svg>
	<span class="name">Missing Tracks</span>
	<SystemNodeActions name="Missing Tracks" nodeId="missing" />
	<span class="count" title={_countTitle()}>{_countText()}</span>
</div>

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
	.row.selected {
		background: var(--rb-select);
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.count {
		flex: none;
		min-width: 4ch;
		align-self: stretch;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
</style>
