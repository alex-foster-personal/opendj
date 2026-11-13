<script lang="ts">
	/**
	 * LIBUX-04 hotkeys overlay. Two views behind a tab control (grid default,
	 * searchable list). Live type-to-filter with yellow match highlighting
	 * in both views. X button always hides; Esc is handled globally by
	 * installHotkeysOverlayHotkeys (#12278: the first Esc clears a filter,
	 * the next one hides). The markup lives in HotkeysOverlayView.svelte
	 * so it can carry a story; this file binds it to the overlay store.
	 * #12271: onHide is the store's hideHotkeysOverlay, so the X button closes
	 * like Esc and "?" and the view's focusTrap hands focus back on unmount.
	 */
	import { assistantSidebarEnabled } from '$lib/assistant/sidebar-flag';
	import { listPerformanceCommands } from '$lib/keyboard/command-registry';
	import {
		ASSISTANT_PLATFORM,
		availableHotkeys,
		HOTKEYS_OVERLAY_TRIGGER,
		overlayHotkeyEntries,
		platformHotkeyEntries
	} from './hotkeys-registry';
	import {
		hideHotkeysOverlay,
		hotkeysOverlay,
		setHotkeysOverlayQuery,
		setHotkeysOverlayView
	} from './hotkeys-overlay.svelte';
	import HotkeysOverlayView from './HotkeysOverlayView.svelte';

	// LIBUX-10456: registry commands with default keys join the hand-written rows.
	// #12254: so do the selector controls, as one group.
	// #12282: rows name this platform's modifier (Cmd on Apple, Ctrl elsewhere); a story or test passes another.
	let { platform = ASSISTANT_PLATFORM }: { platform?: string } = $props();

	const allEntries = overlayHotkeyEntries(listPerformanceCommands());
	const entries = $derived(
		platformHotkeyEntries(availableHotkeys(allEntries, assistantSidebarEnabled()), platform)
	);
</script>

<HotkeysOverlayView
	open={hotkeysOverlay.open}
	view={hotkeysOverlay.view}
	query={hotkeysOverlay.query}
	{entries}
	trigger={HOTKEYS_OVERLAY_TRIGGER}
	onHide={hideHotkeysOverlay}
	onSetView={setHotkeysOverlayView}
	onSetQuery={setHotkeysOverlayQuery}
/>
