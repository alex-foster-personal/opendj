<script lang="ts">
	import { COLUMN_VIEW_PATH, REDO_PATH, TREE_LIST_PATH, UNDO_PATH } from '$lib/ui/icon-glyphs';
	import type { PlaylistTreeViewMode } from '$lib/rb/playlist-tree-view-prefs';
	import { onDestroy } from 'svelte';
	import {
		HISTORY_CLOSE_GRACE_MS,
		HISTORY_HOVER_OPEN_MS,
		HISTORY_NAV_HOVER_MS,
		historyEntryNavTarget,
		historyRowsNewestFirst,
		undoButtonTitle,
		type PlaylistHistoryRow
	} from '$lib/rb/playlist-history';
	import {
		jumpPlaylistHistoryFromChrome,
		playlistHistoryChrome,
		redoPlaylistFromChrome,
		showHistoryTargetFromChrome,
		undoPlaylistFromChrome
	} from '$lib/rb/playlist-history-chrome.svelte';
	import LibrarySourceMenu from './LibrarySourceMenu.svelte';

	export type LibrarySourceTab = 'playlists' | 'taglists' | 'autolists' | 'usbs';

	let {
		active,
		onchange,
		playlistTreeView = 'tree',
		onPlaylistTreeViewChange,
		showPlaylistTools = false
	}: {
		active: LibrarySourceTab;
		onchange: (tab: LibrarySourceTab) => void;
		playlistTreeView?: PlaylistTreeViewMode;
		onPlaylistTreeViewChange?: (mode: PlaylistTreeViewMode) => void;
		showPlaylistTools?: boolean;
	} = $props();

	const tabs: { id: LibrarySourceTab; label: string; testid: string }[] = [
		{ id: 'playlists', label: 'Playlists', testid: 'library-source-playlists' },
		{ id: 'taglists', label: 'Taglists', testid: 'library-source-taglists' },
		{ id: 'autolists', label: 'Autolists', testid: 'library-source-autolists' },
		{ id: 'usbs', label: 'USBs', testid: 'library-source-usbs' }
	];

	let historyOpen = $state(false);
	let historyAnchor = $state<HTMLButtonElement | null>(null);
	let historyPopover = $state<HTMLDivElement | null>(null);
	let openTimer: ReturnType<typeof setTimeout> | null = null;
	let closeTimer: ReturnType<typeof setTimeout> | null = null;
	let navTimer: ReturnType<typeof setTimeout> | null = null;

	function toggleTreeColumn(): void {
		if (!showPlaylistTools || onPlaylistTreeViewChange === undefined) return;
		onPlaylistTreeViewChange(playlistTreeView === 'tree' ? 'column' : 'tree');
	}

	function _clear(timer: ReturnType<typeof setTimeout> | null): null {
		if (timer !== null) clearTimeout(timer);
		return null;
	}

	// LIBUX-8626: 1 s of hover intent opens the history; it then stays open
	// while the pointer is over the undo button OR the popover, so the list
	// can be scrolled and clicked. Leaving both closes it after a short grace
	// that covers the gap between them.
	function intendHistory(): void {
		closeTimer = _clear(closeTimer);
		if (historyOpen || openTimer !== null) return;
		openTimer = setTimeout(() => {
			openTimer = null;
			historyOpen = true;
		}, HISTORY_HOVER_OPEN_MS);
	}

	function openHistoryNow(): void {
		openTimer = _clear(openTimer);
		closeTimer = _clear(closeTimer);
		historyOpen = true;
	}

	// PLAYLIST-ROW-01: only KEYBOARD focus opens the history at once. A mouse
	// click also focuses the button, and opening on that put the popover over
	// the playlist rows right after every undo click, where the pointer on its
	// way to a row kept it open and swallowed the row click.
	function openHistoryOnKeyboardFocus(event: FocusEvent): void {
		const button = event.currentTarget;
		if (!(button instanceof HTMLElement) || !button.matches(':focus-visible')) return;
		openHistoryNow();
	}

	function keepHistoryOpen(): void {
		closeTimer = _clear(closeTimer);
	}

	function releaseHistory(): void {
		openTimer = _clear(openTimer);
		if (!historyOpen || closeTimer !== null) return;
		closeTimer = setTimeout(() => {
			closeTimer = null;
			navTimer = _clear(navTimer);
			historyOpen = false;
		}, HISTORY_CLOSE_GRACE_MS);
	}

	function releaseHistoryOnBlur(event: FocusEvent): void {
		const next = event.relatedTarget;
		if (next instanceof Node && (historyAnchor?.contains(next) || historyPopover?.contains(next))) return;
		releaseHistory();
	}

	function closeHistoryOnEscape(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || !historyOpen) return;
		event.stopPropagation();
		openTimer = _clear(openTimer);
		closeTimer = _clear(closeTimer);
		historyOpen = false;
		historyAnchor?.focus();
	}

	function previewHistoryRow(row: PlaylistHistoryRow): void {
		navTimer = _clear(navTimer);
		const target = historyEntryNavTarget(row);
		if (target === null) return;
		navTimer = setTimeout(() => {
			navTimer = null;
			showHistoryTargetFromChrome(target);
		}, HISTORY_NAV_HOVER_MS);
	}

	async function jumpToHistoryRow(row: PlaylistHistoryRow): Promise<void> {
		navTimer = _clear(navTimer);
		if (!(await jumpPlaylistHistoryFromChrome(row.entry.command_id))) return;
		// After the jump the row flipped applied <-> pending; show where it landed.
		const target = historyEntryNavTarget({ ...row, applied: !row.applied });
		if (target !== null) showHistoryTargetFromChrome(target);
	}

	onDestroy(() => {
		_clear(openTimer);
		_clear(closeTimer);
		_clear(navTimer);
	});

	const historyRows: PlaylistHistoryRow[] = $derived(
		historyRowsNewestFirst(playlistHistoryChrome.entries, playlistHistoryChrome.cursor)
	);

	const undoTitle: string = $derived(
		undoButtonTitle(playlistHistoryChrome.entries, playlistHistoryChrome.cursor, playlistHistoryChrome.canUndo)
	);

	const treeToggleTitle: string = $derived(
		showPlaylistTools
			? playlistTreeView === 'tree'
				? 'Switch playlist sidebar to column browser'
				: 'Switch playlist sidebar to tree list'
			: 'Switch to Playlists to change tree or column layout'
	);
</script>

<div class="source-toolbar">
	<LibrarySourceMenu data-testid="library-source-tabs" data-menu-target="library-source">
		<div class="view-tabs" role="tablist" aria-label="library sources">
			{#each tabs as tab (tab.id)}
				<button
					type="button"
					class="vt"
					class:active={active === tab.id}
					role="tab"
					aria-selected={active === tab.id}
					data-testid={tab.testid}
					onclick={() => onchange(tab.id)}
				>
					{tab.label}
				</button>
			{/each}
		</div>
	</LibrarySourceMenu>
	<div class="trailing-actions" data-testid="library-tree-toolbar">
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-tree-view-toggle"
			title={treeToggleTitle}
			aria-label={treeToggleTitle}
			disabled={!showPlaylistTools}
			aria-pressed={showPlaylistTools && playlistTreeView === 'column'}
			onclick={toggleTreeColumn}
		>
			<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
				<path
					d={playlistTreeView === 'tree' ? COLUMN_VIEW_PATH : TREE_LIST_PATH}
					fill="none"
					stroke="currentColor"
					stroke-width="2"
				/>
			</svg>
		</button>
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-undo"
			aria-label="Undo playlist edit"
			title={undoTitle}
			data-custom-tip=""
			disabled={!playlistHistoryChrome.canUndo}
			bind:this={historyAnchor}
			aria-haspopup="true"
			aria-expanded={historyOpen}
			onclick={() => undoPlaylistFromChrome()}
			onmouseenter={intendHistory}
			onfocus={openHistoryOnKeyboardFocus}
			onmouseleave={releaseHistory}
			onblur={releaseHistoryOnBlur}
			onkeydown={closeHistoryOnEscape}
		>
			<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
				<path d={UNDO_PATH} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
			</svg>
		</button>
		<!-- LIBUX-51: inside .source-toolbar (position: relative) so top: 100% anchors
		     the popover under the undo button; as a sibling it resolved against a page
		     ancestor and opened at the bottom edge of the window (found by the v1 ship
		     day thread, #5674).
		     LIBUX-8626: the full history, newest first, interactive. It only opens
		     after a deliberate 1 s hover (or keyboard focus) and closes shortly after
		     the pointer leaves both it and the button, so it does not linger over the
		     playlist rows it overlaps. -->
		{#if historyOpen && historyAnchor !== null}
			<div
				class="history-popover"
				style:z-index="5"
				role="dialog"
				aria-label="Playlist edit history"
				tabindex="-1"
				data-testid="playlist-history-popover"
				bind:this={historyPopover}
				onmouseenter={keepHistoryOpen}
				onmouseleave={releaseHistory}
				onfocusout={releaseHistoryOnBlur}
				onkeydown={closeHistoryOnEscape}
			>
				<p class="history-hint" data-testid="playlist-undo-next">
					{undoTitle}. Newest first: hover an edit to show it, click to undo back to it.
				</p>
				<ol class="history-list" data-testid="playlist-history-list">
					{#each historyRows as row (row.entry.command_id)}
						<li>
							<button
								type="button"
								class="history-item"
								class:pending={!row.applied}
								data-testid="playlist-history-item"
								data-command-id={row.entry.command_id}
								aria-current={row.next ? 'step' : undefined}
								title={row.action}
								onmouseenter={() => previewHistoryRow(row)}
								onfocus={() => previewHistoryRow(row)}
								onclick={() => void jumpToHistoryRow(row)}
							>
								{row.entry.label}
							</button>
						</li>
					{/each}
				</ol>
			</div>
		{/if}
		<button
			type="button"
			class="icon-btn"
			data-testid="playlist-redo"
			aria-label="Redo playlist edit"
			title="Redo playlist edit (Ctrl/Cmd+Shift+Z)"
			disabled={!playlistHistoryChrome.canRedo}
			onclick={() => redoPlaylistFromChrome()}
		>
			<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
				<path d={REDO_PATH} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
			</svg>
		</button>
	</div>
</div>

<style>
	.source-toolbar {
		display: flex;
		flex: none;
		align-items: stretch;
		border-bottom: 1px solid var(--rb-border);
		position: relative;
		z-index: 2;
	}
	.view-tabs {
		display: flex;
		flex: 1;
		min-width: 0;
	}
	.vt {
		flex: 1;
		padding: 3px 0;
		background: var(--rb-panel);
		border: none;
		border-right: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.vt:last-child {
		border-right: none;
	}
	/* The accent underline REPLACES the toolbar's bottom border under the
	 * active tab (margin -1px lays it over that line) rather than stacking a
	 * second 1px line on it (UIBORDER-02). */
	.vt.active {
		color: var(--rb-text);
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-accent);
		margin-bottom: -1px;
	}
	.trailing-actions {
		display: flex;
		flex: none;
		align-items: center;
		gap: 2px;
		/* 2px top and bottom around the 18px buttons keeps the bar at the
		 * 22px it had when the buttons filled it. */
		padding: 2px 4px;
		border-left: 1px solid var(--rb-border);
		background: var(--rb-panel);
	}
	/* 18px tall, not the bar's full 22px: a full-height button's top and
	 * bottom borders sat on the panel's top border and the toolbar's bottom
	 * border, two 2px lines (UIBORDER-02). */
	.icon-btn {
		width: 22px;
		height: 18px;
		padding: 0;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
	}
	.icon-btn svg {
		display: block;
		margin: auto;
	}
	.icon-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.icon-btn:not(:disabled):hover {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
	}
	.history-popover {
		position: absolute;
		top: 100%;
		right: 4px;
		margin-top: 2px;
		min-width: 200px;
		max-width: 320px;
		padding: 4px 6px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
	}
	.history-hint {
		margin: 0 0 4px;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.history-list {
		margin: 0;
		padding: 0;
		max-height: min(320px, 60vh);
		overflow-y: auto;
		overscroll-behavior: contain;
		list-style: none;
	}
	.history-item {
		display: block;
		width: 100%;
		border: none;
		background: none;
		text-align: left;
		cursor: pointer;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
		padding: 1px 2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.history-item:hover,
	.history-item:focus-visible {
		background: var(--rb-panel);
		outline: 1px solid var(--rb-accent);
		outline-offset: -1px;
	}
	.history-item.pending {
		color: var(--rb-text-dim);
		text-decoration: line-through;
	}
	.history-item[aria-current='step'] {
		color: var(--rb-accent);
	}
</style>
