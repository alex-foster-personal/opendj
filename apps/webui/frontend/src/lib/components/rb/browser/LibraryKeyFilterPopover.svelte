<script lang="ts" module>
	/** Boot hydration reads the filter for the first mount; later mounts
	 * (returning to the route) re-read it, since events missed while unmounted
	 * are not replayed (Codex P2, #7743). */
	let mountedBefore = false;
</script>

<script lang="ts">
	import type { Component } from 'svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import {
		libraryKeyFilterSync,
		rereadLibraryKeyFilter,
		setLibraryKeyFilter,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		resolveLibraryKeyFilterReference,
		type LibraryKeyFilterDeckSlice
	} from '$lib/rb/library-key-filter';
	import type { KeyNotation } from '$lib/rb/key-notation';

	type PanelComponent = Component<{
		referenceKey: string | null;
		keys: readonly string[];
		notation: KeyNotation;
		save: (keys: readonly string[]) => Promise<void>;
		onClose: () => void;
	}>;

	let {
		selectedRowKey,
		decks
	}: {
		/** The library's selected row key (raw); the Compatible reference when it parses. */
		selectedRowKey: string | null;
		/** Every deck's master/playing/key, the fallback reference. */
		decks: readonly LibraryKeyFilterDeckSlice[];
	} = $props();

	/** Selected row first, else a playing deck (master first). */
	const referenceKey = $derived(resolveLibraryKeyFilterReference(selectedRowKey, decks));

	// A filter set by an agent (POST /api/v1/ui-prefs/library-key-filter) or
	// another window reaches this page's table on the ui_prefs event, and on a
	// bus resync, which is all a reconnect delivers for writes it missed.
	$effect(() => {
		if (mountedBefore) rereadLibraryKeyFilter();
		mountedBefore = true;
	});
	$effect(() => subscribeKind('ui_prefs', rereadLibraryKeyFilter));
	$effect(() => subscribeResync(rereadLibraryKeyFilter));

	let open = $state(false);
	let Panel = $state<PanelComponent | null>(null);
	let panelLoad: Promise<void> | null = null;
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const selectedCount = $derived(uiPrefs.library_key_filter.length);
	const triggerLabel = $derived(
		selectedCount === 0 ? 'Key' : `Key (${selectedCount})`
	);
	const syncError = $derived(libraryKeyFilterSync.error);

	function _ensurePanel(): void {
		if (Panel !== null || panelLoad !== null) return;
		panelLoad = import('./LibraryKeyFilterPanel.svelte').then((mod) => {
			Panel = mod.default;
		});
	}

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function _toggle(): void {
		if (open) {
			_close();
			return;
		}
		_ensurePanel();
		open = true;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		event.preventDefault();
		_close();
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (!open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (triggerEl?.contains(target)) return;
		if (popoverEl?.contains(target)) return;
		_close();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<div class="library-key-filter-pop">
	<button
		type="button"
		class="rb-lit-button library-key-filter-trigger"
		class:lit={selectedCount > 0 || open}
		class:sync-error={syncError !== null}
		aria-label={syncError === null ? undefined : `${triggerLabel}: ${syncError}`}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		title={syncError ?? 'Filter the library by Camelot keys'}
		onclick={_toggle}
	>
		{triggerLabel}{#if syncError !== null}&nbsp;!{/if}
	</button>
	{#if open && Panel !== null}
		<div
			class="library-key-filter-float"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<Panel
				{referenceKey}
				keys={uiPrefs.library_key_filter}
				notation={uiPrefs.key_notation}
				save={setLibraryKeyFilter}
				onClose={_close}
			/>
		</div>
	{/if}
</div>

<style>
	.library-key-filter-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
	}

	.library-key-filter-float {
		z-index: 40;
	}

	/* Chrome comes from the shared .rb-lit-button, like the header's other
	 * buttons; its own taller box doubled the browser panel's top border. */
	.library-key-filter-trigger {
		white-space: nowrap;
	}

	.library-key-filter-trigger.sync-error {
		color: var(--rb-danger, #f66);
	}
</style>
