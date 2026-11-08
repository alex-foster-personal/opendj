<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { ContextMenuItem } from '../ContextMenu.svelte';
	import ContextMenu from '../ContextMenu.svelte';

	let {
		tag = 'div',
		class: className = '',
		children,
		...restProps
	}: {
		tag?: keyof HTMLElementTagNameMap;
		class?: string;
		children: Snippet;
	} & Record<string, unknown> = $props();

	let menu = $state<{ x: number; y: number } | null>(null);
	let items = $state<ContextMenuItem[]>([]);

	async function openMenu(e: MouseEvent): Promise<void> {
		e.preventDefault();
		e.stopPropagation();
		menu = { x: e.clientX, y: e.clientY };
		items = [];
		const { librarySourceMenuItems } = await import('./library-source-menu-actions');
		const next = await librarySourceMenuItems();
		if (next !== null) items = next;
	}

	async function openFromKeyboard(e: KeyboardEvent): Promise<void> {
		if (e.key !== 'ContextMenu' && !(e.shiftKey && e.key === 'F10')) return;
		e.preventDefault();
		e.stopPropagation();
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8 };
		items = [];
		const { librarySourceMenuItems } = await import('./library-source-menu-actions');
		const next = await librarySourceMenuItems();
		if (next !== null) items = next;
	}
</script>

<svelte:element
	this={tag}
	class={className}
	oncontextmenu={openMenu}
	onkeydown={openFromKeyboard}
	{...restProps}
>
	{@render children()}
</svelte:element>

{#if menu !== null && items.length > 0}
	<ContextMenu {items} x={menu.x} y={menu.y} onclose={() => (menu = null)} />
{/if}
