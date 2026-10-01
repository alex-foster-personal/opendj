<script lang="ts">
	/**
	 * Lazy mount point for the performance quick-draw menu (PR #4011).
	 *
	 * /performance renders this on every load, but the menu itself only exists
	 * after a right-click inside .perf-root. A static import put the menu, its
	 * load-blend controller, its HUD and the stem ladder in the boot bundle for
	 * every session and took `performance` over its budget, so the menu module
	 * is fetched by the first right-click that asks for it, the pattern
	 * MidiPanelLoader uses for the MIDI drawer (#3896).
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ The menu module is not imported until the first right-click inside
	 *     .perf-root, and nothing is rendered before then.
	 *     [if] the /performance boot chunk still statically imports QuickDrawMenu [then] ⛔️
	 *     [if] a right-click outside .perf-root fetches the menu [then] ⛔️
	 *   ✔︎ The right-click that triggers the fetch opens the menu once it lands,
	 *     at that click's point and for that click's target, and the native
	 *     context menu is suppressed while it loads.
	 *     [if] the first right-click shows the browser's own menu [then] ⛔️
	 *     [if] the first right-click opens nothing and only the second works [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline with the error text, never a toast and
	 *     never swallowed, and it is recovered by a fresh document on the
	 *     user's click. A same-document retry cannot work, because the browser
	 *     keeps the failed fetch in its module map and a second import() of
	 *     that URL rejects without touching the network (see retrySetupOverlay
	 *     in routes/+layout.svelte), so the import is tried once per document.
	 *     [if] the import rejects and the right-click shows nothing [then] ⛔️
	 *     [if] the error promises a retry on reopen, or reloads unasked [then] ⛔️
	 */
	import type { Component } from 'svelte';

	let QuickDrawMenuComponent: Component<{ initialEvent?: MouseEvent | null }> | null =
		$state(null);
	let loadError: string | null = $state(null);
	let errorVisible = $state(false);
	/** The latest right-click made before the module landed; the menu replays it. */
	let pendingEvent: MouseEvent | null = $state(null);
	let requested = false;

	function onContextMenu(e: MouseEvent): void {
		// Once loaded, the menu owns its own window listener.
		if (QuickDrawMenuComponent !== null) return;
		const t = e.target;
		if (!(t instanceof Element) || t.closest('.perf-root') === null) return;
		e.preventDefault();
		e.stopPropagation();
		if (loadError !== null) {
			errorVisible = true;
			return;
		}
		pendingEvent = e;
		if (requested) return;
		requested = true;
		import('$lib/components/rb/QuickDrawMenu.svelte')
			.then((m) => {
				QuickDrawMenuComponent = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[quick-draw] quick-draw menu failed to load', exc);
				loadError = exc instanceof Error ? exc.message : String(exc);
				pendingEvent = null;
				errorVisible = true;
			});
	}
</script>

<svelte:window oncontextmenu={onContextMenu} />

{#if QuickDrawMenuComponent}
	<QuickDrawMenuComponent initialEvent={pendingEvent} />
{:else if errorVisible && loadError !== null}
	<div class="qd-load-error rb-panel" role="alert" data-testid="quick-draw-menu-load-error">
		<span>Quick-draw menu failed to load: {loadError}</span>
		<button type="button" class="qd-load-close" onclick={() => location.reload()}>Reload</button>
		<button type="button" class="qd-load-close" onclick={() => (errorVisible = false)}>Close</button>
	</div>
{/if}

<style>
	.qd-load-error {
		position: fixed;
		top: 48px;
		right: 12px;
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
	.qd-load-close {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		padding: 2px 8px;
		cursor: pointer;
	}
</style>
