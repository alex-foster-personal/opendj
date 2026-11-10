<script module lang="ts">
	import type { Component } from 'svelte';
	import { createLazyOnceImport } from '$lib/components/rb/lazy-once-import';

	// <script module> runs once per document, so the panel module is fetched at most once.
	const panelImport = createLazyOnceImport<Component>(
		() => import('$lib/components/rb/TrackLoadResetPanel.svelte'),
		(exc) => console.error('[track-load-reset] reset panel failed to load', exc)
	);
</script>

<script lang="ts">
	/**
	 * Lazy mount point for the track load reset panel (PERF-BOOT-11728, issue #10247).
	 *
	 * /performance renders this on every load, but the panel only shows after a deck or
	 * channel strip RST chip flips trackLoadResetOpen(). A static import put the panel in
	 * the boot bundle (the +3 KiB raise of PR #7691); this pays it back, the same way
	 * QuickDrawMenuLoader and MidiPanelLoader do.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ The panel module is not imported until trackLoadResetOpen() is true.
	 *     [if] the /performance page still statically imports TrackLoadResetPanel [then] ⛔️
	 *   ✔︎ The first open shows the panel as soon as the module lands, no second click.
	 *     [if] the first open renders nothing after the import resolves [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline by the chip with its error text and offers
	 *     Reload on click; never a toast.
	 *     [if] the import rejects and the open shows nothing, or a toast [then] ⛔️
	 *   ✔︎ The import is tried once per document: the browser keeps a failed fetch in its
	 *     module map, so a same-document attempt cannot succeed.
	 *     [if] a second open imports again, or the page reloads unasked [then] ⛔️
	 */
	import {
		closeTrackLoadReset,
		trackLoadResetOpen,
		trackLoadResetTrigger
	} from '$lib/rb/track-load-reset.svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

	let PanelComponent: Component | null = $state(null);
	let loadError: string | null = $state(null);
	const open = $derived(trackLoadResetOpen());

	$effect(() => {
		if (!open || PanelComponent !== null || loadError !== null) return;
		void panelImport.request().then((r) => {
			if (r.ok) PanelComponent = r.value;
			else loadError = r.error;
		});
	});
</script>

{#if PanelComponent}
	<PanelComponent />
{:else if open && loadError !== null}
	<div
		class="tlr-load-error rb-panel"
		role="alert"
		data-testid="track-load-reset-panel-load-error"
		use:triggerFloatingAction={{
			getTrigger: () => trackLoadResetTrigger(),
			preferred: 'above',
			gap: 4
		}}
	>
		<span>Reset panel failed to load: {loadError}</span>
		<button type="button" class="tlr-load-close" onclick={() => location.reload()}>Reload</button>
		<button type="button" class="tlr-load-close" onclick={closeTrackLoadReset}>Close</button>
	</div>
{/if}

<style>
	.tlr-load-error {
		position: fixed;
		z-index: 60;
		display: flex;
		gap: 10px;
		align-items: center;
		max-width: 420px;
		padding: 8px 12px;
		border: 1px solid var(--rb-red);
		color: var(--rb-text);
		font-size: 12px;
		overflow-wrap: anywhere;
	}
	.tlr-load-close {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		padding: 2px 8px;
		cursor: pointer;
	}
</style>
