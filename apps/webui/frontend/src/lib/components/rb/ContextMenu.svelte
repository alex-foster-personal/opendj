<script lang="ts">
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';

	export interface ContextMenuItem {
		id: string;
		label: string;
		run?: (() => void | Promise<void>) | undefined;
		title?: string;
		testId?: string;
		checked?: boolean;
	}

	let { items, x, y, onclose }: {
		items: ContextMenuItem[];
		x: number;
		y: number;
		onclose: () => void;
	} = $props();

	let menu = $state<HTMLDivElement | null>(null);
	let position = $state({ x: 0, y: 0 });

	async function placeMenu(): Promise<void> {
		await tick();
		if (menu === null) return;
		const rect = menu.getBoundingClientRect();
		position = clampToViewport(
			x,
			y,
			{ width: rect.width, height: rect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		menu.focus();
	}

	$effect(() => {
		void x;
		void y;
		void placeMenu();
	});

	async function activate(item: ContextMenuItem): Promise<void> {
		if (item.run === undefined) return;
		onclose();
		await item.run();
	}

	function menuItems(): HTMLElement[] {
		if (menu === null) return [];
		return Array.from(
			menu.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemcheckbox"]')
		);
	}

	function focusMenuItem(index: number): void {
		const items = menuItems();
		if (items.length === 0) return;
		const wrapped = ((index % items.length) + items.length) % items.length;
		items[wrapped].focus();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			onclose();
			return;
		}

		const target = event.target;
		if (
			target instanceof HTMLElement &&
			target.getAttribute('aria-disabled') === 'true' &&
			(event.key === 'Enter' || event.key === ' ')
		) {
			event.preventDefault();
			return;
		}

		if (menu === null || !menu.contains(document.activeElement)) return;

		const items = menuItems();
		if (items.length === 0) return;

		const active = document.activeElement;
		const currentIndex = active instanceof HTMLElement ? items.indexOf(active) : -1;

		if (event.key === 'ArrowDown') {
			event.preventDefault();
			focusMenuItem(currentIndex < 0 ? 0 : currentIndex + 1);
			return;
		}
		if (event.key === 'ArrowUp') {
			event.preventDefault();
			focusMenuItem(currentIndex < 0 ? items.length - 1 : currentIndex - 1);
			return;
		}
		if (event.key === 'Home') {
			event.preventDefault();
			focusMenuItem(0);
			return;
		}
		if (event.key === 'End') {
			event.preventDefault();
			focusMenuItem(items.length - 1);
		}
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) onclose();
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div bind:this={menu} class="context-menu" data-testid="context-menu" role="menu" tabindex="-1" style={`left:${position.x}px;top:${position.y}px`}>
	{#each items as item (item.id)}
		{@const unavailable = item.run === undefined}
		{@const explanation = item.title ?? (unavailable ? plannedTitle('context-menu-unavailable') : item.label)}
		{@const descId = `ctx-menu-desc-${item.id}`}
		<button
			type="button"
			role={typeof item.checked === 'boolean' ? 'menuitemcheckbox' : 'menuitem'}
			data-testid={item.testId}
			aria-disabled={unavailable ? true : undefined}
			aria-checked={typeof item.checked === 'boolean' ? item.checked : undefined}
			aria-describedby={unavailable ? descId : undefined}
			title={explanation}
			onclick={() => void activate(item)}
		>{item.label}</button>
		{#if unavailable}
			<span id={descId} class="visually-hidden">{explanation}</span>
		{/if}
	{/each}
</div>

<style>
	.context-menu { position: fixed; z-index: 1000; min-width: 190px; max-width: calc(100vw - 16px); max-height: calc(100vh - 16px); overflow: auto; padding: 4px; border: 1px solid var(--rb-border); border-radius: 3px; background: var(--rb-panel-raised); box-shadow: 0 5px 18px rgb(0 0 0 / 45%); }
	.context-menu button { display: block; width: 100%; padding: 5px 8px; border: 0; border-radius: 2px; background: transparent; color: var(--rb-text); font: inherit; text-align: left; }
	.context-menu button:not([aria-disabled='true']):hover { background: var(--rb-select); }
	.context-menu button:focus-visible { background: var(--rb-select); }
	.context-menu button[aria-disabled='true'] { color: var(--rb-text-dim); cursor: not-allowed; }
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
