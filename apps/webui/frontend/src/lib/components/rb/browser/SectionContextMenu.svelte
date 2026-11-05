<script lang="ts">
	import ContextMenu from '../ContextMenu.svelte';
	import { bindMenuItems } from '$lib/context-menu/bind';
	import { loadMenuRegistry, menuRegistry, preloadMenuRegistry } from '$lib/context-menu/lazy.svelte';
	import { reportMenuLoadFailure } from './track-context-menu';

	/** A divider's right-click menu. Items come from the W28 registry
	 * ((private path)); this only binds their runners. */
	let {
		editable,
		onrename,
		oninsertabove,
		onremove
	}: {
		editable: boolean;
		onrename: () => void;
		oninsertabove: () => void;
		onremove: () => void;
	} = $props();

	let menu = $state<{ x: number; y: number } | null>(null);

	$effect(preloadMenuRegistry);

	export function open(event: MouseEvent): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY };
	}
</script>

{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={bindMenuItems(menuRegistry()!.menuFor({ kind: 'playlist-section' }, { editable }), {
			rename: onrename,
			'insert-section-above': oninsertabove,
			'remove-divider': onremove
		})}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
