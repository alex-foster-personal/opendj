<script lang="ts">
	// Generic DOM-list virtualization (browser-surface unit, shared by
	// ColumnBrowser's three columns - track-list-virtualization's fixed-row
	// window math (virtual-window.ts) factored out of TrackTable so a
	// second surface doesn't have to re-derive it. TrackTable keeps its own
	// inline copy (it interleaves virtualization with per-row
	// IntersectionObserver hydration, which doesn't fit a snippet-based
	// generic list cleanly); this component is for simpler item lists.
	import type { Snippet } from 'svelte';
	import { computeVirtualWindow } from './virtual-window';

	let {
		itemCount,
		rowHeight,
		overscan = 15,
		children
	}: {
		itemCount: number;
		/** Must match the rendered row's fixed CSS height exactly - see
		 * computeVirtualWindow's docs (fixed row height is what makes the
		 * spacer approach exact rather than approximate). */
		rowHeight: number;
		overscan?: number;
		/** Renders rows[startIndex, endIndex) - the parent owns the actual
		 * item array and slices it itself. */
		children: Snippet<[number, number]>;
	} = $props();

	let scrollEl = $state<HTMLDivElement | null>(null);
	let scrollTop = $state(0);
	let viewportHeight = $state(0);

	$effect(() => {
		const el = scrollEl;
		if (el === null) return;
		viewportHeight = el.clientHeight;
		if (typeof ResizeObserver === 'undefined') return; // SSR guard
		const ro = new ResizeObserver((entries) => {
			for (const entry of entries) viewportHeight = entry.contentRect.height;
		});
		ro.observe(el);
		return () => ro.disconnect();
	});

	const windowInfo = $derived(
		computeVirtualWindow({ scrollTop, viewportHeight, rowHeight, rowCount: itemCount, overscan })
	);
</script>

<div
	class="vlist-scroll"
	bind:this={scrollEl}
	onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}
>
	{#if windowInfo.topPad > 0}
		<div class="vlist-spacer" style={`height:${windowInfo.topPad}px`} aria-hidden="true"></div>
	{/if}
	{@render children(windowInfo.startIndex, windowInfo.endIndex)}
	{#if windowInfo.bottomPad > 0}
		<div class="vlist-spacer" style={`height:${windowInfo.bottomPad}px`} aria-hidden="true"></div>
	{/if}
</div>

<style>
	.vlist-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
</style>
