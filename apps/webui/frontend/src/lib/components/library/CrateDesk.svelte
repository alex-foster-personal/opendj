<script lang="ts">
	// LIBWS-01: Library mode's Crate Desk. The same browser Gig mounts under
	// its decks gets the whole window here, with a preview bar above it and a
	// track inspector beside it. Loaded lazily by routes/+page.svelte so a cold
	// open of "/" (which PERFMODE-11 sends on to Gig) does not download it.
	// Palette is scoped under .perf-root (theme.css); never touch :root.
	import '$lib/rb/theme.css';
	import BrowserPanel from '$lib/components/rb/BrowserPanel.svelte';
	import LibraryPreviewBar from './LibraryPreviewBar.svelte';
	import LibraryTopBar from './LibraryTopBar.svelte';
	import TrackInspector from './TrackInspector.svelte';
	import CrateDeskView from './CrateDeskView.svelte';
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { libraryWorkspace, publishLibrarySelection } from '$lib/library/library-workspace.svelte';
	import {
		gigDeckLoadHref,
		libraryAudioRefusal,
		libraryPreviewPlayer,
		previewLibraryRow,
		stopLibraryPreview
	} from '$lib/library/library-preview.svelte';
	import { createLibraryPreviewIpc, installLibraryPreviewIpc } from '$lib/library/library-preview-ipc';
	import { setAppMode } from '$lib/rb/prefs.svelte';
	import { gigRuntimeReleaseSettled, markLibraryModeExit } from '$lib/rb/library-mode-runtime';

	// LIBWS-01/04/06: the browser learns it is in Library mode from this prop,
	// from its first render: it publishes its selection here, plays previews
	// through the engine-free preview player, and hands deck loads to Gig.
	const library = {
		publish: publishLibrarySelection,
		preview: previewLibraryRow,
		async loadToDeck(row: { stable_id: string }, deck: 1 | 2 | 3 | 4 | null): Promise<void> {
			// Gig comes back with empty decks (PERFMODE-14), so "next free" is 1.
			stopLibraryPreview();
			// However Library mode was entered (picker, persisted mode, reload),
			// Gig must not replay the session from before it (PERFMODE-14).
			markLibraryModeExit();
			// Entered from a running Gig, its teardown may still be disposing the
			// engine: remounting Gig before it settles strands the old decks and
			// drops this load (Codex P1, PR #6189).
			await gigRuntimeReleaseSettled();
			setAppMode('performance');
			await goto(gigDeckLoadHref(row.stable_id, deck ?? 1));
		}
	};

	// Agents drive the same preview through window.musicDjToolsLibraryPreview
	// (LIBWS-04 agent parity); a selected row's refusal applies to them too.
	onMount(() =>
		installLibraryPreviewIpc(
			createLibraryPreviewIpc({
				player: libraryPreviewPlayer,
				refusal: (id) => {
					const row = libraryWorkspace.selected_rows.find((r) => r.stable_id === id);
					return row === undefined ? null : libraryAudioRefusal(row);
				}
			})
		)
	);

	// The preview's AudioContext and decoded audio never outlive the desk.
	onDestroy(stopLibraryPreview);
</script>

<CrateDeskView>
	{#snippet topbar()}<LibraryTopBar />{/snippet}
	{#snippet preview()}<LibraryPreviewBar />{/snippet}
	{#snippet browser()}<BrowserPanel {library} />{/snippet}
	{#snippet inspector()}<TrackInspector />{/snippet}
</CrateDeskView>
