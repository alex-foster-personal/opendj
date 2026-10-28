<script lang="ts">
	/**
	 * Lazy mount point for the USB export screen (USBEXP-01). The screen is
	 * fetched by the first open, the MidiPanelLoader / QuickDrawMenuLoader
	 * pattern, so it costs the boot bundle nothing. A failed fetch is shown
	 * inline with its error, never swallowed; the browser keeps a failed
	 * import in its module map, so it is tried once per document.
	 *
	 * While a plan, write or eject runs, an in-app navigation that would
	 * unmount this loader (a link, history back or forward, an agent's shell
	 * move, or a reload or window close) is cancelled and the screen says why, so the operation's
	 * progress and result are never orphaned.
	 */
	import type { Component } from 'svelte';
	import { beforeNavigate } from '$app/navigation';

	import { closeUsbExport, refuseNavigationWhileUsbBusy, usbExport } from '$lib/rb/usb-export.svelte';
	import { loadUsbExportScreen } from '$lib/rb/usb-export-screen-load';

	let Screen: Component | null = $state(null);
	let loadError: string | null = $state(null);
	let requested = false;

	beforeNavigate((navigation) => {
		// A reload, a window close or an external link ('leave') is refused
		// too: the browser shows its own leave confirmation, and in the
		// desktop shell the close is held until the operation settles. A reload
		// keeps its route id, so the unload flag, not the ids, decides it.
		if (
			refuseNavigationWhileUsbBusy(
				navigation.from?.route.id ?? null,
				navigation.to?.route.id ?? null,
				navigation.willUnload
			)
		) {
			navigation.cancel();
		}
	});

	$effect(() => {
		if (!usbExport.open || requested) return;
		requested = true;
		loadUsbExportScreen()
			.then((component) => {
				Screen = component;
			})
			.catch((exc: unknown) => {
				console.error('[usb-export] screen failed to load', exc);
				loadError = exc instanceof Error ? exc.message : String(exc);
			});
	});
</script>

{#if usbExport.open}
	{#if Screen !== null}
		<Screen />
	{:else if loadError !== null}
		<div class="ux-load-error" role="alert" data-testid="usb-export-load-error">
			USB export screen failed to load: {loadError}. Reload the window to try again.
			<button type="button" onclick={closeUsbExport}>Close</button>
		</div>
	{/if}
{/if}

<style>
	.ux-load-error {
		position: fixed;
		left: 50%;
		top: 50%;
		transform: translate(-50%, -50%);
		z-index: 61;
		padding: 10px 12px;
		background: var(--rb-panel-raised, #1a1f28);
		border: 1px solid var(--rb-red, #e0594f);
		color: var(--rb-text);
		font-size: 12px;
	}
</style>
