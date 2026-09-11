<script lang="ts">
	import { tick } from 'svelte';

	export interface ContextMenuItem {
		id: string;
		label: string;
		run?: (() => void | Promise<void>) | undefined;
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
		position = {
			x: Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)),
			y: Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))
		};
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

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			onclose();
		}
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) onclose();
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div bind:this={menu} class="context-menu" data-testid="context-menu" role="menu" tabindex="-1" style={`left:${position.x}px;top:${position.y}px`}>
	{#each items as item (item.id)}
		<button type="button" role="menuitem" disabled={item.run === undefined} title={item.run === undefined ? 'not implemented - see PARITY-TODO' : item.label} onclick={() => void activate(item)}>{item.label}</button>
	{/each}
</div>

<style>
	.context-menu { position: fixed; z-index: 1000; min-width: 190px; max-width: calc(100vw - 16px); max-height: calc(100vh - 16px); overflow: auto; padding: 4px; border: 1px solid var(--rb-border); border-radius: 3px; background: var(--rb-panel-raised); box-shadow: 0 5px 18px rgb(0 0 0 / 45%); }
	.context-menu button { display: block; width: 100%; padding: 5px 8px; border: 0; border-radius: 2px; background: transparent; color: var(--rb-text); font: inherit; text-align: left; }
	.context-menu button:not(:disabled):hover, .context-menu button:not(:disabled):focus-visible { background: var(--rb-select); }
	.context-menu button:disabled { color: var(--rb-text-dim); cursor: not-allowed; }
</style>
