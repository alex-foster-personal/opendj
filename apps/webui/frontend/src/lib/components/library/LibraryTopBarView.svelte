<script lang="ts">
	// Props-only half of LibraryTopBar (LIBWS-01): the mode picker, the pages
	// menu, the inert quick selector and the library readouts. The container
	// reads the route, the shell-nav context, the selection and health, and
	// passes the CloudSync chip (a container) in as a snippet.
	import type { Snippet } from 'svelte';
	import { APP_MODES, type AppMode } from '$lib/rb/app-mode';
	import ModeThumbnail from '$lib/components/rb/ModeThumbnail.svelte';
	import { isCurrentNavLink, type SidebarNavLink } from '$lib/shell/sidebar-nav';

	let {
		activeMode,
		pageLinks,
		pathname,
		selectedCount,
		libraryCounts,
		selectMode,
		modeForHref,
		addMusicLabel,
		addMusicTitle,
		addMusicBlocked,
		onAddMusic,
		cloudSyncChip
	}: {
		activeMode: AppMode;
		/** The app-shell sidebar's links, from the shell-nav context. */
		pageLinks: SidebarNavLink[];
		/** The route on screen, for aria-current. */
		pathname: string;
		selectedCount: number;
		/** Library database counts; null until health has answered. */
		libraryCounts: { tracks: number; playlists: number } | null;
		selectMode: (event: MouseEvent, mode: (typeof APP_MODES)[number]) => void | Promise<void>;
		modeForHref: (href: string) => (typeof APP_MODES)[number] | undefined;
		addMusicLabel: string;
		addMusicTitle: string;
		addMusicBlocked: string | null;
		onAddMusic: () => void;
		cloudSyncChip: Snippet;
	} = $props();

	const INERT = 'not implemented - see PARITY-TODO';

	let modePickerEl: HTMLDetailsElement | undefined = $state();
	let modeMenuEl: HTMLDivElement | undefined = $state();
	let pagesMenuEl: HTMLDetailsElement | undefined = $state();

	function _dismissModeMenuOnOutsidePointer(event: PointerEvent): void {
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (pagesMenuEl?.open && !pagesMenuEl.contains(target)) pagesMenuEl.open = false;
		if (modePickerEl === undefined || !modePickerEl.open) return;
		if (modePickerEl.contains(target) || modeMenuEl?.contains(target)) return;
		modePickerEl.open = false;
	}

	function _dismissModeMenuOnEscape(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		if (pagesMenuEl !== undefined) pagesMenuEl.open = false;
		if (modePickerEl === undefined) return;
		modePickerEl.open = false;
	}
</script>

<svelte:window onpointerdown={_dismissModeMenuOnOutsidePointer} onkeydown={_dismissModeMenuOnEscape} />

<header class="library-topbar rb-panel" data-testid="library-topbar">
	<details class="mode-picker" bind:this={modePickerEl}>
		<summary class="mode-dd" aria-label="Choose app mode" title="Choose app mode">
			{activeMode.label.toUpperCase()}
			<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
				<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
			</svg>
		</summary>
		<div class="mode-menu" bind:this={modeMenuEl} aria-label="App modes">
			<p class="mode-menu-heading">Choose app mode</p>
			{#each APP_MODES as mode (mode.id)}
				<a
					class="mode-card"
					data-testid="mode-card"
					href={mode.href}
					aria-current={mode.id === activeMode.id ? 'page' : undefined}
					onclick={(event) => void selectMode(event, mode)}
				>
					<ModeThumbnail iconId={mode.iconId} />
					<span class="mode-copy">
						<strong>{mode.label}</strong>
						<span class="mode-gain" data-testid="mode-gain">{mode.gain}</span>
						<span class="mode-lose" data-testid="mode-lose">{mode.lose}</span>
					</span>
				</a>
			{/each}
		</div>
	</details>

	<details class="pages-menu" data-testid="library-pages-menu" bind:this={pagesMenuEl}>
		<summary class="mode-dd" aria-label="Go to page" title="Go to another page (the app-shell sidebar's links)">
			PAGES
			<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
				<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
			</svg>
		</summary>
		<nav class="pages-list" aria-label="Pages">
			{#each pageLinks as link (link.href)}
				<a
					href={link.href}
					title={link.title}
					aria-current={isCurrentNavLink(link.href, pathname) ? 'page' : undefined}
					onclick={(event) => {
						if (pagesMenuEl !== undefined) pagesMenuEl.open = false;
						const mode = modeForHref(link.href);
						if (mode !== undefined) void selectMode(event, mode);
					}}
				>{link.label}</a>
			{/each}
		</nav>
	</details>

	<!-- LIBWS-05: the Cmd-K quick track selector is planned, not built. -->
	<button
		class="quick-select rb-inert"
		data-testid="library-quick-select"
		disabled
		title={INERT}
		aria-label={INERT}
	>
		<svg width="11" height="11" viewBox="0 0 11 11" aria-hidden="true">
			<circle cx="4.5" cy="4.5" r="3.5" fill="none" stroke="currentColor" stroke-width="1.2" />
			<path d="M7.2 7.2 L10 10" stroke="currentColor" stroke-width="1.2" />
		</svg>
		<span>Quick track selector</span>
		<kbd>⌘K</kbd>
	</button>

	<span class="spacer"></span>

	<button
		type="button"
		class="add-music"
		class:prominent={libraryCounts !== null && libraryCounts.tracks === 0}
		data-testid="library-add-music"
		disabled={addMusicBlocked !== null}
		title={addMusicBlocked ?? addMusicTitle}
		onclick={() => onAddMusic()}
	>
		{addMusicLabel}
	</button>

	{#if libraryCounts}
		<span class="readout" title="Tracks in the library database">{libraryCounts.tracks} tracks</span>
		<span class="readout" title="Playlists in the library database">{libraryCounts.playlists} playlists</span>
	{/if}
	<span
		class="selection-count"
		data-testid="library-selection-count"
		title="Tracks selected in the active browser tab"
	>{selectedCount} selected</span>
	<!-- SETNOTE-6340g: upgraded by the set-notes app once it loads (lib/set-notes/toggle-element.ts). -->
	<set-notes-toggle></set-notes-toggle>
	{@render cloudSyncChip()}
</header>

<style>
	.library-topbar {
		grid-area: topbar;
		display: flex;
		align-items: center;
		gap: 10px;
		height: var(--rb-topbar-h);
		padding: 0 8px;
		border-bottom: 1px solid var(--rb-border);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
	}
	.mode-dd {
		display: flex;
		align-items: center;
		gap: 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.05em;
		padding: 2px 8px;
		line-height: 1;
		cursor: pointer;
		list-style: none;
	}
	.mode-dd::-webkit-details-marker {
		display: none;
	}
	.mode-picker {
		position: relative;
		z-index: 20;
	}
	.pages-menu {
		position: relative;
		z-index: 20;
	}
	.pages-menu[open] > .mode-dd {
		border-color: var(--rb-accent);
	}
	.pages-list {
		position: absolute;
		z-index: 100;
		top: calc(100% + 5px);
		left: 0;
		display: grid;
		min-width: 160px;
		padding: 4px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}
	.pages-list a {
		padding: 4px 6px;
		border-radius: 2px;
		color: var(--rb-text);
		text-decoration: none;
	}
	.pages-list a:hover,
	.pages-list a:focus-visible,
	.pages-list a[aria-current='page'] {
		background: color-mix(in srgb, var(--rb-accent) 12%, transparent);
		outline: none;
	}
	.mode-picker[open] > .mode-dd {
		border-color: var(--rb-accent);
	}
	.mode-menu {
		position: absolute;
		z-index: 100;
		top: calc(100% + 5px);
		left: 0;
		width: 300px;
		padding: 7px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}
	.mode-menu-heading {
		margin: 1px 3px 6px;
		color: var(--rb-text-dim);
		font-size: 9px;
		font-weight: 650;
		letter-spacing: 0.07em;
		text-transform: uppercase;
	}
	.mode-card {
		display: grid;
		grid-template-columns: 52px minmax(0, 1fr);
		align-items: center;
		gap: 7px;
		min-height: 48px;
		padding: 5px;
		border: 1px solid transparent;
		border-radius: 2px;
		color: var(--rb-text);
		text-decoration: none;
	}
	a.mode-card:hover,
	a.mode-card:focus-visible,
	a.mode-card[aria-current='page'] {
		border-color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-accent) 12%, transparent);
		outline: none;
	}
	.mode-copy {
		display: grid;
		gap: 2px;
		min-width: 0;
		font-size: 9px;
		line-height: 1.25;
	}
	.mode-copy strong {
		font-size: 10px;
		letter-spacing: 0.03em;
	}
	.mode-lose {
		color: var(--rb-text-dim);
	}
	.quick-select {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 280px;
		height: 20px;
		padding: 0 8px;
		background: var(--rb-bg);
		border: 1px solid var(--rb-border);
		border-radius: 10px;
		color: var(--rb-text-dim);
		font: inherit;
	}
	.quick-select span {
		flex: 1;
		text-align: left;
	}
	.quick-select kbd {
		font-family: inherit;
		font-size: 9px;
		padding: 0 4px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
	}
	.spacer {
		flex: 1;
	}
	.add-music {
		display: flex;
		align-items: center;
		height: 20px;
		padding: 2px 8px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font: inherit;
		font-size: var(--rb-fs-label);
		line-height: 1;
		cursor: pointer;
	}
	.add-music:disabled {
		cursor: not-allowed;
		color: var(--rb-text-dim);
	}
	.add-music.prominent {
		border-color: var(--rb-accent);
		color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-accent) 12%, var(--rb-panel-raised));
	}
	.readout,
	.selection-count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	/* SETNOTE-6340g: holds the toggle's size so the bar does not shift when it is upgraded. */
	set-notes-toggle {
		display: inline-flex;
		flex: 0 0 22px;
		height: 20px;
	}
</style>
