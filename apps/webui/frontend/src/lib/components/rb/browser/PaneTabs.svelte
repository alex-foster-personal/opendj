<script module lang="ts">
	/** Display info for one browser pane tab. */
	export interface PaneTabInfo {
		/** Playlist name, or 'blank list' when the pane has no selection. */
		title: string;
		/** Hydrated row count; null while loading or when no playlist chosen. */
		count: number | null;
		/** True while the pane is hydrating its playlist. */
		loading: boolean;
	}
</script>

<script lang="ts">
	// 4-pane browser tabs (SCREENSHOT-SPEC 5c). Active tab shows
	// 'Name (N Tracks)' + inert stepper chevrons; the other panes each
	// remember their own playlist selection (only the active pane renders).
	let {
		tabs,
		active,
		onactivate
	}: {
		tabs: PaneTabInfo[];
		active: number;
		onactivate: (index: number) => void;
	} = $props();

	function _label(tab: PaneTabInfo): string {
		if (tab.loading) return `${tab.title} (loading...)`;
		else if (tab.count !== null) return `${tab.title} (${tab.count} Tracks)`;
		else return tab.title;
	}
</script>

<div class="tabs" role="tablist" aria-label="browser panes">
	{#each tabs as tab, i (i)}
		{#if i === active}
			<div class="tab active" role="tab" aria-selected="true">
				<span class="title">{_label(tab)}</span>
				<!-- up/down stepper: crisp SVG chevrons (SCREENSHOT-SPEC 5c) -->
				<span class="stepper">
					<button
						class="rb-inert"
						disabled
						title="not implemented - see PARITY-TODO"
						aria-label="previous track in pane"
					>
						<svg viewBox="0 0 8 5" width="8" height="5" aria-hidden="true">
							<path
								d="M1 4 L4 1 L7 4"
								fill="none"
								stroke="currentColor"
								stroke-width="1.3"
							/>
						</svg>
					</button>
					<button
						class="rb-inert"
						disabled
						title="not implemented - see PARITY-TODO"
						aria-label="next track in pane"
					>
						<svg viewBox="0 0 8 5" width="8" height="5" aria-hidden="true">
							<path
								d="M1 1 L4 4 L7 1"
								fill="none"
								stroke="currentColor"
								stroke-width="1.3"
							/>
						</svg>
					</button>
				</span>
			</div>
		{:else}
			<button class="tab" role="tab" aria-selected="false" onclick={() => onactivate(i)}>
				<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
					<path d="M2 3h12v2H2zM2 7h12v2H2zM2 11h12v2H2z" fill="currentColor" />
				</svg>
				<span class="title">{_label(tab)}</span>
			</button>
		{/if}
	{/each}
</div>

<style>
	.tabs {
		display: flex;
		align-items: stretch;
		min-width: 0;
		overflow: hidden;
	}
	.tab {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 0 10px;
		background: var(--rb-panel);
		border: none;
		border-right: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		white-space: nowrap;
		cursor: pointer;
	}
	.tab.active {
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		cursor: default;
	}
	.tab svg {
		flex: none;
	}
	.title {
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.stepper {
		display: inline-flex;
		flex-direction: column;
		margin-left: 4px;
	}
	.stepper button {
		display: flex;
		align-items: center;
		height: 8px;
		padding: 0 2px;
		background: transparent;
		border: none;
		color: var(--rb-text);
		line-height: 1;
	}
</style>
