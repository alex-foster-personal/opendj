<script lang="ts">
	// LIBUX-49 (the maintainer, Tue 6 Oct 2026, B7): the library's tag-edit buttons
	// (Find & Replace, Bulk Edit, MyTags) wrapped the one-line pane header onto
	// several lines. They live behind ONE pencil now, and its menu opens on click.
	//
	// The menu is always mounted and only shown while open, so every item stays
	// in the DOM (the LESS view hides Find & Replace and Bulk Edit through
	// `data-less-hidden`, from performance/+page.svelte, hidden and never
	// removed). The list is position: fixed and placed from the pencil's rect,
	// because the library panel clips overflow.
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

	let {
		hasSelection,
		onpick
	}: { hasSelection: boolean; onpick: (action: 'find-replace' | 'bulk-edit' | 'mytag') => void } = $props();

	const NEEDS_SELECTION = 'Select one or more tracks first';

	let open = $state(false);
	let wrapEl: HTMLSpanElement | undefined = $state();
	let triggerEl: HTMLButtonElement | undefined = $state();

	function pick(action: 'find-replace' | 'bulk-edit' | 'mytag'): void {
		open = false;
		onpick(action);
	}

	function onWindowPointerDown(e: PointerEvent): void {
		if (!open) return;
		if (e.target instanceof Node && wrapEl?.contains(e.target)) return;
		open = false;
	}

	function onWindowKeyDown(e: KeyboardEvent): void {
		if (!open || e.key !== 'Escape') return;
		open = false;
		triggerEl?.focus();
	}
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeyDown} />

<span class="library-edit-menu" bind:this={wrapEl}>
	<button
		bind:this={triggerEl}
		type="button"
		class="rb-lit-button edit-trigger"
		aria-label="Edit tags"
		aria-haspopup="menu"
		aria-expanded={open}
		title="Edit tags - Find & Replace and Bulk Edit for the selected tracks, and MyTags"
		data-testid="library-edit-menu"
		onclick={() => (open = !open)}
	>
		<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
			<path
				d="M11.3 1.6a1.6 1.6 0 0 1 2.3 0l.8.8a1.6 1.6 0 0 1 0 2.3L5.6 13.5l-3.6.9.9-3.6z"
				fill="none"
				stroke="currentColor"
				stroke-width="1.4"
				stroke-linejoin="round"
			/>
			<path d="M9.8 3.1l3.1 3.1" stroke="currentColor" stroke-width="1.4" />
		</svg>
	</button>
	<div
		class="edit-menu-list"
		class:open
		role="menu"
		aria-label="Edit tags"
		data-testid="library-edit-menu-list"
		use:triggerFloatingAction={{ getTrigger: () => triggerEl ?? null, preferred: 'below', gap: 4 }}
	>
		<button
			type="button"
			class="rb-lit-button"
			role="menuitem"
			data-less-hidden
			disabled={!hasSelection}
			title={hasSelection ? undefined : NEEDS_SELECTION}
			onclick={() => pick('find-replace')}
		>
			Find &amp; Replace
		</button>
		<button
			type="button"
			class="rb-lit-button"
			role="menuitem"
			data-less-hidden
			disabled={!hasSelection}
			title={hasSelection ? undefined : NEEDS_SELECTION}
			onclick={() => pick('bulk-edit')}
		>
			Bulk Edit
		</button>
		<button type="button" class="rb-lit-button" role="menuitem" onclick={() => pick('mytag')}>MyTags</button>
	</div>
</span>

<style>
	.library-edit-menu {
		display: inline-flex;
		flex: none;
	}
	.edit-trigger {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0 6px;
	}
	.edit-menu-list {
		display: none;
		z-index: 40;
		flex-direction: column;
		align-items: stretch;
		gap: 3px;
		padding: 4px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: var(--rb-panel);
		box-shadow: 0 4px 14px rgb(0 0 0 / 0.45);
	}
	.edit-menu-list.open {
		display: flex;
	}
</style>
