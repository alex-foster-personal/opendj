<script lang="ts">
	// LIBWS-01: the Library workspace top bar. Same mode picker and chrome as
	// Gig's TopBar, without any deck, mixer or engine control: Library mode
	// has none of those running.
	import { getContext } from 'svelte';
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { APP_MODES, appModeForPath } from '$lib/rb/app-mode';
	import { SHELL_NAV_CONTEXT, type ShellNav } from '$lib/shell/sidebar-nav';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import {
		gigRuntimeReleasePending,
		gigRuntimeReleaseSettled,
		markLibraryModeExit
	} from '$lib/rb/library-mode-runtime';
	import { holdLibraryExitForGigRelease } from '$lib/library/library-exit-gate';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import { health, pushToast } from '$lib/stores.svelte';
	import {
		ADD_MUSIC_LABEL,
		ADD_MUSIC_TITLE,
		runSetup,
		runSetupBlocked
	} from '$lib/setup/run-setup';
	import LibraryTopBarView from './LibraryTopBarView.svelte';

	// Main has no app-shell sidebar beside a full-bleed route (PERF-UI-07
	// removed the compact app nav), so the Crate Desk carries the sidebar's own
	// links here: the pages it lists stay reachable from Library mode without
	// a typed URL. The root layout provides the same list (and the mode
	// switch) as context, so the developer-page rule stays in one place.
	const shellNav = getContext<ShellNav>(SHELL_NAV_CONTEXT);
	const pageLinks = $derived(shellNav.links);

	const activeMode = $derived(appModeForPath($page.url.pathname));
	const selectedCount = $derived(libraryWorkspace.selected_ids.length);
	const libraryCounts = $derived(
		health.data
			? { tracks: health.data.state_db.tracks, playlists: health.data.state_db.playlists }
			: null
	);
	const addMusicBlocked = $derived(runSetupBlocked());

	async function onAddMusic(): Promise<void> {
		const failure = await runSetup(goto);
		if (failure !== null) pushToast(`Could not reopen setup: ${failure}`, 'error');
	}

	/**
	 * LIBWS-06: leaving Library through its own picker. Library may have been
	 * entered without the Gig picker (a typed `/`, a link, a reload), so this
	 * exit marks itself: the next Gig must not replay its old session or rescue
	 * ring. Like Load to deck, it waits for a Gig teardown still in flight
	 * before Gig remounts, or the remount would race that release (Codex P1s,
	 * PR #6189). A modified click opens elsewhere and is left to the browser.
	 */
	async function selectMode(event: MouseEvent, mode: (typeof APP_MODES)[number]): Promise<void> {
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
		const mode = modeForHref(to.url.pathname);
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
	function modeForHref(href: string): (typeof APP_MODES)[number] | undefined {
		return APP_MODES.find((mode) => mode.href === href);
	}
</script>

<LibraryTopBarView
	{activeMode}
	{pageLinks}
	pathname={$page.url.pathname}
	{selectedCount}
	{libraryCounts}
	addMusicLabel={ADD_MUSIC_LABEL}
	addMusicTitle={ADD_MUSIC_TITLE}
	{addMusicBlocked}
	onAddMusic={() => void onAddMusic()}
	{selectMode}
	{modeForHref}
>
	{#snippet cloudSyncChip()}<CloudSyncStatusChip />{/snippet}
</LibraryTopBarView>
