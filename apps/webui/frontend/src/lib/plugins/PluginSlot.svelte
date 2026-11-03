<script lang="ts">
	/**
	 * A named place an installed module may draw (see slots.svelte.ts). Renders
	 * nothing at all when no loaded module fills it, so the core layout is
	 * unchanged for a user with no plugins.
	 */
	import { onMount } from 'svelte';
	import { slotKey } from './slot-keys';
	import { mountSlot, pluginSlots } from './slots.svelte';

	let { name }: { name: string } = $props();

	const filled = $derived(pluginSlots.for(name));

	onMount(() => {
		void pluginSlots.load();
	});

	function host(el: HTMLElement, slot: (typeof filled)[number]) {
		let cleanup: (() => void) | null = null;
		let gone = false;
		void mountSlot(el, slot).then((fn) => {
			if (gone) fn?.();
			else cleanup = fn;
		});
		return {
			destroy() {
				gone = true;
				cleanup?.();
			}
		};
	}
</script>

{#each filled as slot (slotKey(slot))}
	<span class="plugin-slot" data-slot={name} data-plugin={slot.plugin_id} use:host={slot}></span>
{/each}

<style>
	.plugin-slot {
		display: contents;
	}
</style>
