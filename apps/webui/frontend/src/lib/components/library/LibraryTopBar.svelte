<script lang="ts">
	// LIBWS-01: the Library workspace top bar. Same mode picker and chrome as
	// Gig's TopBar, without any deck, mixer or engine control: Library mode
	// has none of those running.
	import { getContext } from 'svelte';
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { APP_MODES, appModeForPath } from '$lib/rb/app-mode';
	import { modeIconClass } from '$lib/rb/app-mode-icons';
	import { isCurrentNavLink, SHELL_NAV_CONTEXT, type ShellNav } from '$lib/shell/sidebar-nav';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import {
		gigRuntimeReleasePending,
		gigRuntimeReleaseSettled,
		markLibraryModeExit
	} from '$lib/rb/library-mode-runtime';
	import { holdLibraryExitForGigRelease } from '$lib/library/library-exit-gate';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import { health } from '$lib/stores.svelte';

	const INERT = 'not implemented - see PARITY-TODO';

	let modePickerEl: HTMLDetailsElement | undefined = $state();
	let modeMenuEl: HTMLDivElement | undefined = $state();
	let pagesMenuEl: HTMLDetailsElement | undefined = $state();

	// Main has no app-shell sidebar beside a full-bleed route (PERF-UI-07
	// removed the compact app nav), so the Crate Desk carries the sidebar's own
	// links here: the pages it lists stay reachable from Library mode without
	// a typed URL. The root layout provides the same list (and the mode
	// switch) as context, so the developer-page rule stays in one place.
	const shellNav = getContext<ShellNav>(SHELL_NAV_CONTEXT);
	const pageLinks = $derived(shellNav.links);

	const activeMode = $derived(appModeForPath($page.url.pathname));
	const selectedCount = $derived(libraryWorkspace.selected_ids.length);

	/**
	 * LIBWS-06: leaving Library through its own picker. Library may have been
	 * entered without the Gig picker (a typed `/`, a link, a reload), so this
	 * exit marks itself: the next Gig must not replay its old session or rescue
	 * ring. Like Load to deck, it waits for a Gig teardown still in flight
	 * before Gig remounts, or the remount would race that release (Codex P1s,
	 * PR #6189). A modified click opens elsewhere and is left to the browser.
	 */
	async function _selectMode(event: MouseEvent, mode: (typeof APP_MODES)[number]): Promise<void> {
		if (mode.id === 'library') {
			shellNav.selectMode(mode.id);
			return;
		}
		markLibraryModeExit();
		const plainClick =
			event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
		if (!plainClick) {
			shellNav.selectMode(mode.id);
			return;
		}
		event.preventDefault();
		await gigRuntimeReleaseSettled();
		shellNav.selectMode(mode.id);
		await goto(mode.href);
	}

	// Any other client-side way out to a mode route (browser Back or Forward, a
	// goto from elsewhere) still marks the exit, so Gig skips its old session,
	// and waits out a Gig teardown still in flight the way the picker does.
	beforeNavigate((navigation) => {
		const { to } = navigation;
		if (to === null || to.route.id === null) return;
		const mode = _modeForHref(to.url.pathname);
		if (mode === undefined || mode.id === 'library') return;
		markLibraryModeExit();
		holdLibraryExitForGigRelease(navigation, {
			pending: gigRuntimeReleasePending,
			settled: gigRuntimeReleaseSettled,
			go: (delta) => history.go(delta),
			goto: (url) => goto(url)
		});
	});

	/** A page link that opens a mode route leaves Library the same guarded way. */
	function _modeForHref(href: string): (typeof APP_MODES)[number] | undefined {
		return APP_MODES.find((mode) => mode.href === href);
	}

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
					onclick={(event) => void _selectMode(event, mode)}
				>
					<span class={`mode-thumbnail ${modeIconClass(mode.iconId)}`} aria-hidden="true"></span>
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
					aria-current={isCurrentNavLink(link.href, $page.url.pathname) ? 'page' : undefined}
					onclick={(event) => {
						if (pagesMenuEl !== undefined) pagesMenuEl.open = false;
						const mode = _modeForHref(link.href);
						if (mode !== undefined) void _selectMode(event, mode);
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

	{#if health.data}
		<span class="readout" title="Tracks in the library database">{health.data.state_db.tracks} tracks</span>
		<span class="readout" title="Playlists in the library database">{health.data.state_db.playlists} playlists</span>
	{/if}
	<span
		class="selection-count"
		data-testid="library-selection-count"
		title="Tracks selected in the active browser tab"
	>{selectedCount} selected</span>
	<CloudSyncStatusChip />
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
	.mode-thumbnail {
		display: block;
		height: 36px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		background-color: #141920;
	}
	.mode-thumbnail.gig {
		background:
			linear-gradient(90deg, transparent 48%, #72b9ff 48% 52%, transparent 52%),
			linear-gradient(#161d26 45%, #72b9ff 45% 52%, #161d26 52%);
	}
	.mode-thumbnail.prep {
		background:
			linear-gradient(180deg, #72b9ff 0 18%, transparent 18% 82%, #346c3e 82% 100%),
			repeating-linear-gradient(90deg, #1a222c 0 4px, #161d26 4px 8px);
	}
	.mode-thumbnail.library {
		background:
			linear-gradient(90deg, #3b79ad 0 22%, transparent 22% 28%, #346c3e 28% 50%, transparent 50% 56%, #7a5c33 56% 78%, transparent 78%),
			#161d26;
	}
	.mode-thumbnail.trackify {
		background:
			radial-gradient(circle at 50% 50%, #a8b2bf 0 12%, #303b48 13% 31%, #72b9ff 32% 36%, #161d26 37%),
			linear-gradient(90deg, transparent 42%, #72b9ff 42% 58%, transparent 58%);
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
	.readout,
	.selection-count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
</style>
