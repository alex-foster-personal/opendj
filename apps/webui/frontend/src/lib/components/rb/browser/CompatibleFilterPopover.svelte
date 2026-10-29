<script lang="ts">
	import type { Component, Snippet } from 'svelte';
	import type { CompatibleMode } from '$lib/rb/compatible-filter-prefs';
	import { PendingKeyBuffer } from './pending-key-buffer';

	type PanelComponent = Component<
		{ onEnter: () => void; onLeave: () => void },
		{ handleKey: (e: KeyboardEvent) => void }
	>;

	// The mode comes from the parent, which already reads the prefs store, so
	// this component adds no importer to prefs.svelte.ts (quality fan-in ratchet).
	let { children, mode }: { children: Snippet; mode: CompatibleMode } = $props();
	let open = $state(false);
	let closeTimer: ReturnType<typeof setTimeout> | undefined;
	let openTimer: ReturnType<typeof setTimeout> | undefined;
	/** KEYSHIFT-05 (the maintainer, Tue 13 Oct 2026): the dropdown opens on a 70 ms hover
	 * debounce, so a pointer passing over the checkbox does not flash it open.
	 * Keyboard focus opens it at once. */
	const OPEN_DEBOUNCE_MS = 70;
	// The range panel only renders on hover, so it loads on first open rather
	// than riding the /performance route's eager bundle budget.
	let Panel = $state<PanelComponent | null>(null);
	let panelLoad: Promise<void> | null = null;
	let panelRef = $state<{ handleKey: (e: KeyboardEvent) => void } | undefined>(undefined);
	// Keys pressed before the lazy panel binds are held and replayed once it does.
	const pendingKeys = new PendingKeyBuffer<KeyboardEvent>();
	$effect(() => {
		const panel = panelRef;
		if (panel !== undefined) pendingKeys.flush((e) => panel.handleKey(e));
	});
	$effect(() => {
		if (!open) pendingKeys.clear();
	});

	function _ensurePanel(): void {
		if (Panel !== null || panelLoad !== null) return;
		panelLoad = import('./CompatibleFilterPanel.svelte').then((mod) => {
			Panel = mod.default;
		});
	}

	function _clearCloseTimer(): void {
		if (closeTimer !== undefined) {
			clearTimeout(closeTimer);
			closeTimer = undefined;
		}
	}

	function _clearOpenTimer(): void {
		if (openTimer !== undefined) {
			clearTimeout(openTimer);
			openTimer = undefined;
		}
	}

	function _open(): void {
		_clearCloseTimer();
		_clearOpenTimer();
		_ensurePanel();
		open = true;
	}

	function _openSoon(): void {
		_clearCloseTimer();
		if (open || openTimer !== undefined) return;
		_ensurePanel();
		openTimer = setTimeout(() => {
			openTimer = undefined;
			open = true;
		}, OPEN_DEBOUNCE_MS);
	}

	/** Keyboard focus lands on the checkbox, which sits outside the panel, so the
	 * dropdown's T / M and A/D/W/S keys are handled here for the whole wrapper,
	 * panel included. Handled once: the panel has no listener of its own. */
	function _onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape' && open) {
			_clearOpenTimer();
			pendingKeys.clear();
			open = false;
			return;
		}
		if (!open) return;
		if (panelRef === undefined) {
			pendingKeys.hold(e);
			return;
		}
		panelRef.handleKey(e);
	}

	function _scheduleClose(): void {
		_clearOpenTimer();
		_clearCloseTimer();
		closeTimer = setTimeout(() => {
			open = false;
			closeTimer = undefined;
		}, 120);
	}
</script>

<div
	class="compat-filter-pop"
	data-owns-letter-keys=""
	data-custom-tip=""
	onmouseenter={_openSoon}
	onmouseleave={_scheduleClose}
	onfocusin={_open}
	onfocusout={_scheduleClose}
	onkeydown={_onKeydown}
>
	{@render children()}
	<span
		class="compat-mode-badge"
		aria-hidden="true"
		data-mode={mode}
	>{mode === 'mashup' ? 'MSH' : 'TRN'}</span>
	{#if open && Panel !== null}
		<Panel bind:this={panelRef} onEnter={_clearCloseTimer} onLeave={_scheduleClose} />
	{/if}
</div>

<style>
	.compat-mode-badge {
		font-family: ui-monospace, Menlo, monospace;
		font-size: 10px;
		letter-spacing: 0.06em;
		line-height: 1.3;
		padding: 0 4px;
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		color: var(--rb-text-dim, #838990);
	}
	/* Header room: see the KEYSHIFT-05 note in BrowserPanel.svelte. */
	@container browser-header (max-width: 863.98px) {
		.compat-mode-badge {
			display: none;
		}
	}
	.compat-filter-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
	}
</style>
