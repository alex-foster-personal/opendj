<script module lang="ts">
	/** Display info for one browser pane tab. */
	export interface PaneTabInfo {
		/** Playlist name, or 'blank list' when the pane has no selection. */
		title: string;
		/** Hydrated row count; null while loading or when no playlist chosen. */
		count: number | null;
		/** True while the pane is hydrating its playlist. */
		loading: boolean;
		/** Locked: a playlist selection opens in another pane, not this one. */
		sticky: boolean;
		/** No playlist loaded, so its rows can still be saved as a new one. */
		ephemeral: boolean;
	}
</script>

<script lang="ts">
	// 4-pane browser tabs (SCREENSHOT-SPEC 5c). Active tab shows
	// 'Name (N Tracks)' + inert stepper chevrons; the other panes each
	// remember their own playlist selection (only the active pane renders).
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import {
		canAddPaneSlot,
		MAX_PANE_SLOTS
	} from './pane-tabs';
	import {
		decodePlaylistDrag,
		PLAYLIST_DRAG_MIME,
		type PlaylistDragPayload
	} from './playlist-drag';

	let {
		tabs,
		active,
		onactivate,
		ontogglesticky,
		onreorder,
		ondropplaylist,
		onsaveas,
		onaddpane
	}: {
		tabs: PaneTabInfo[];
		active: number;
		onactivate: (index: number) => void;
		/** Lock/unlock a tab so playlist selections route around it. */
		ontogglesticky: (index: number) => void;
		/** Tab dragged from one slot to another. */
		onreorder: (from: number, to: number) => void;
		/** Playlist dragged out of the tree and dropped on the tab bar. */
		ondropplaylist: (payload: PlaylistDragPayload) => void;
		/** Persist an ephemeral pane's rows as a real playlist. */
		onsaveas: (index: number) => void;
		/** Open another blank pane slot (up to MAX_PANE_SLOTS). */
		onaddpane?: () => void;
	} = $props();

	/** Index of the tab being dragged; null when no tab drag is in flight. */
	let dragFrom: number | null = $state(null);
	/** Index the pointer is currently over, for the drop-target outline. */
	let dragOver: number | null = $state(null);

	function _label(tab: PaneTabInfo): string {
		if (tab.loading) return `${tab.title} (loading...)`;
		else if (tab.count !== null) return `${tab.title} (${tab.count} Tracks)`;
		else return tab.title;
	}

	function _onTabDragStart(event: DragEvent, index: number): void {
		dragFrom = index;
		event.dataTransfer?.setData('text/plain', String(index));
		if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = 'move';
	}

	function _onTabDragOver(event: DragEvent, index: number): void {
		// Accept a tab reorder or a playlist from the tree; ignore anything
		// else so a foreign drag keeps the browser's own cursor.
		const types = event.dataTransfer?.types ?? [];
		const isPlaylist = [...types].includes(PLAYLIST_DRAG_MIME);
		if (dragFrom === null && !isPlaylist) return;
		event.preventDefault();
		dragOver = index;
	}

	function _onTabDrop(event: DragEvent, index: number): void {
		event.preventDefault();
		dragOver = null;
		const raw = event.dataTransfer?.getData(PLAYLIST_DRAG_MIME) ?? '';
		const payload = raw === '' ? null : decodePlaylistDrag(raw);
		if (payload !== null) {
			dragFrom = null;
			ondropplaylist(payload);
			return;
		}
		const from = dragFrom;
		dragFrom = null;
		if (from === null || from === index) return;
		onreorder(from, index);
	}

	function _onTabDragEnd(): void {
		dragFrom = null;
		dragOver = null;
	}
</script>

<div class="tabs" role="tablist" aria-label="browser panes">
	{#each tabs as tab, i (i)}
		{#if i === active}
			<div
				class="tab active"
				class:drop-target={dragOver === i}
				role="tab"
				aria-selected="true"
				tabindex={0}
				draggable="true"
				ondragstart={(e) => _onTabDragStart(e, i)}
				ondragover={(e) => _onTabDragOver(e, i)}
				ondrop={(e) => _onTabDrop(e, i)}
				ondragend={_onTabDragEnd}
			>
				<button
					class="lock"
					class:locked={tab.sticky}
					onclick={() => ontogglesticky(i)}
					aria-pressed={tab.sticky}
					title={tab.sticky
						? 'tab locked - unlock to let playlist clicks replace it'
						: 'lock this tab so playlist clicks open elsewhere'}
					aria-label={tab.sticky ? 'unlock this pane tab' : 'lock this pane tab'}
				>
					<svg viewBox="0 0 10 12" width="9" height="10" aria-hidden="true">
						<rect x="1" y="5" width="8" height="6" rx="1" fill="currentColor" />
						{#if tab.sticky}
							<path
								d="M3 5 V3.4 a2 2 0 0 1 4 0 V5"
								fill="none"
								stroke="currentColor"
								stroke-width="1.2"
							/>
						{:else}
							<path
								d="M3 5 V3.4 a2 2 0 0 1 4 0"
								fill="none"
								stroke="currentColor"
								stroke-width="1.2"
							/>
						{/if}
					</svg>
				</button>
				<span class="title">{_label(tab)}</span>
				{#if tab.ephemeral}
					<button
						class="save-as"
						onclick={() => onsaveas(i)}
						title="save this blank list's rows as a real playlist"
						aria-label="save pane as playlist"
					>
						save as
					</button>
				{:else}
					<!-- up/down stepper: crisp SVG chevrons (SCREENSHOT-SPEC 5c) -->
					<span class="stepper">
						<button
							class="rb-inert"
							disabled
							title={plannedTitle('pane-prev-track')}
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
							title={plannedTitle('pane-next-track')}
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
				{/if}
			</div>
		{:else}
			<button
				class="tab"
				class:drop-target={dragOver === i}
				role="tab"
				aria-selected="false"
				onclick={() => onactivate(i)}
				draggable="true"
				ondragstart={(e) => _onTabDragStart(e, i)}
				ondragover={(e) => _onTabDragOver(e, i)}
				ondrop={(e) => _onTabDrop(e, i)}
				ondragend={_onTabDragEnd}
			>
				<!-- Inactive tabs stay a single button, so the lock is a static
				     indicator here; the toggle lives on the active tab. -->
				{#if tab.sticky}
					<svg viewBox="0 0 10 12" width="9" height="10" aria-label="locked">
						<rect x="1" y="5" width="8" height="6" rx="1" fill="currentColor" />
						<path
							d="M3 5 V3.4 a2 2 0 0 1 4 0 V5"
							fill="none"
							stroke="currentColor"
							stroke-width="1.2"
						/>
					</svg>
				{:else}
					<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
						<path d="M2 3h12v2H2zM2 7h12v2H2zM2 11h12v2H2z" fill="currentColor" />
					</svg>
				{/if}
				<span class="title">{_label(tab)}</span>
			</button>
		{/if}
	{/each}
	{#if onaddpane !== undefined && canAddPaneSlot(tabs.length)}
		<button class="tab add-pane" type="button" onclick={() => onaddpane?.()} aria-label="Blank List (+)">
			Blank List (+)
		</button>
	{/if}
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
		max-height: 24px;
		overflow: hidden;
	}
	.tab.add-pane {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
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
	.tab.drop-target {
		box-shadow: inset 2px 0 0 0 var(--rb-accent);
	}
	.lock {
		display: flex;
		flex: none;
		align-items: center;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.lock.locked {
		color: var(--rb-accent);
	}
	.save-as {
		flex: none;
		margin-left: 4px;
		padding: 0 4px;
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.save-as:hover {
		color: var(--rb-text);
	}
</style>
