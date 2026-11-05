<script lang="ts">
	/**
	 * SETNOTE-6340e: the editor's right-click menu. Suggests tracks for the
	 * selected text straight away (the same search the `@` popover and Cmd K
	 * use), lets the search be refined, and offers Bold plus, on a link, open
	 * and remove. Escape or a click outside closes it.
	 */
	import { onMount, untrack } from 'svelte';
	import type { TrackHit } from './store.svelte';
	import { createTrackSearch, hitLabel } from './track-search.svelte';

	let {
		x,
		y,
		text,
		canLink,
		onLink,
		onBold,
		linkActions,
		onClose
	}: {
		x: number;
		y: number;
		text: string;
		canLink: boolean;
		onLink: (hit: TrackHit) => void;
		onBold: () => void;
		linkActions: { open: () => void; remove: () => void } | null;
		onClose: () => void;
	} = $props();

	const SUGGESTIONS = 5;
	const search = createTrackSearch(SUGGESTIONS);
	let menu = $state<HTMLDivElement | null>(null);
	let q = $state(untrack(() => text));

	onMount(() => {
		search.query(text, true);
		const onPointer = (e: PointerEvent): void => {
			if (menu && !menu.contains(e.target as Node)) onClose();
		};
		const onKey = (e: KeyboardEvent): void => {
			if (e.key !== 'Escape') return;
			e.preventDefault();
			e.stopPropagation();
			onClose();
		};
		window.addEventListener('pointerdown', onPointer, true);
		window.addEventListener('keydown', onKey, true);
		return () => {
			search.cancel();
			window.removeEventListener('pointerdown', onPointer, true);
			window.removeEventListener('keydown', onKey, true);
		};
	});
</script>

<div
	bind:this={menu}
	class="sn-menu"
	data-testid="set-notes-context-menu"
	role="menu"
	tabindex="-1"
	style:left="{Math.min(x, window.innerWidth - 260)}px"
	style:top="{Math.min(y, window.innerHeight - 240)}px"
>
	{#each search.state.hits as hit (hit.stable_id)}
		<button
			type="button"
			role="menuitem"
			data-testid="set-notes-link-suggestion"
			disabled={!canLink}
			title={canLink
				? `Link the selected text to ${hitLabel(hit)}`
				: 'Select some text to link first'}
			onclick={() => onLink(hit)}>Link to: {hitLabel(hit)}</button
		>
	{:else}
		<button type="button" role="menuitem" disabled>
			{search.state.error
				? `Search failed: ${search.state.error}`
				: search.state.searched || !q.trim()
					? 'No matching tracks'
					: 'Searching...'}
		</button>
	{/each}
	<input
		type="search"
		class="sn-menu-search"
		data-testid="set-notes-link-search"
		placeholder="Search tracks..."
		bind:value={q}
		oninput={() => search.query(q)}
	/>
	<hr />
	<button type="button" role="menuitem" onclick={onBold}>Bold</button>
	{#if linkActions}
		<button
			type="button"
			role="menuitem"
			data-testid="set-notes-link-open"
			onclick={linkActions.open}>Open / load link</button
		>
		<button
			type="button"
			role="menuitem"
			data-testid="set-notes-link-remove"
			onclick={linkActions.remove}>Remove link</button
		>
	{/if}
</div>

<style>
	.sn-menu {
		position: fixed;
		z-index: 360;
		display: flex;
		flex-direction: column;
		width: 250px;
		padding: 3px;
		background: var(--sn-soft);
		color: var(--sn-ink);
		border: 1px solid var(--sn-line);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
		font-size: 12px;
	}
	.sn-menu button {
		text-align: left;
		background: transparent;
		color: inherit;
		border: 0;
		padding: 3px 6px;
		cursor: pointer;
		font: inherit;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sn-menu button:hover:not(:disabled) {
		background: var(--sn-hl);
	}
	.sn-menu button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.sn-menu-search {
		margin: 3px 2px;
		font: inherit;
		border: 1px solid var(--sn-line);
		background: var(--sn-paper);
		color: inherit;
	}
	.sn-menu hr {
		width: 100%;
		border: 0;
		border-top: 1px solid var(--sn-line);
		margin: 2px 0;
	}
</style>
