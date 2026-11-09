<script lang="ts">
	/**
	 * LIBUX-04 hotkeys overlay. Two views behind a tab control (grid default,
	 * searchable list). Live type-to-filter with yellow match highlighting
	 * in both views. X button always hides; Esc is handled globally by
	 * installHotkeysOverlayHotkeys. The markup lives in HotkeysOverlayView.svelte
	 * so it can carry a story; this file binds it to the overlay store.
	 */
	import { assistantSidebarEnabled } from '$lib/assistant/sidebar-flag';
	import { listPerformanceCommands } from '$lib/keyboard/command-registry';
	import {
		availableHotkeys,
		HOTKEY_REGISTRY,
		hotkeyEntriesFromCommandRegistry,
		mergeHotkeyEntries
	} from './hotkeys-registry';
	import {
		hideHotkeysOverlay,
		hotkeysOverlay,
		setHotkeysOverlayQuery,
		setHotkeysOverlayView
	} from './hotkeys-overlay.svelte';
	import HotkeysOverlayView from './HotkeysOverlayView.svelte';

	// LIBUX-10456: registry commands with default keys join the hand-written rows.
	const allEntries = mergeHotkeyEntries(
		HOTKEY_REGISTRY,
		hotkeyEntriesFromCommandRegistry(listPerformanceCommands())
	);
	const entries = $derived(availableHotkeys(allEntries, assistantSidebarEnabled()));
</script>

<HotkeysOverlayView
	open={hotkeysOverlay.open}
	view={hotkeysOverlay.view}
	query={hotkeysOverlay.query}
	{entries}
	onHide={hideHotkeysOverlay}
	onSetView={setHotkeysOverlayView}
	onSetQuery={setHotkeysOverlayQuery}
/>
