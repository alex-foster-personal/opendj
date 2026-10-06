<script lang="ts">
	import type { Component, Snippet } from 'svelte';

	type PanelComponent = Component<{ onEnter: () => void; onLeave: () => void }>;

	let { children }: { children: Snippet } = $props();
	let open = $state(false);
	let closeTimer: ReturnType<typeof setTimeout> | undefined;
	// The range panel only renders on hover, so it loads on first open rather
	// than riding the /performance route's eager bundle budget.
	let Panel = $state<PanelComponent | null>(null);
	let panelLoad: Promise<void> | null = null;

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

	function _open(): void {
		_clearCloseTimer();
		_ensurePanel();
		open = true;
	}

	function _scheduleClose(): void {
		_clearCloseTimer();
		closeTimer = setTimeout(() => {
			open = false;
			closeTimer = undefined;
		}, 120);
	}
</script>

<div
	class="compat-filter-pop"
	data-custom-tip=""
	onmouseenter={_open}
	onmouseleave={_scheduleClose}
	onfocusin={_open}
	onfocusout={_scheduleClose}
>
	{@render children()}
	{#if open && Panel !== null}
		<Panel onEnter={_clearCloseTimer} onLeave={_scheduleClose} />
	{/if}
</div>

<style>
	.compat-filter-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
	}
</style>
