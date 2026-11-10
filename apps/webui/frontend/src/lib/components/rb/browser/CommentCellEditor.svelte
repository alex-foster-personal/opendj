<script lang="ts">
	// LIBUX-56: the library Comment cell's inline editor. Enter or blur saves,
	// Escape cancels. The table mounts it in place of the cell text, on a
	// double-click of the cell or the row menu's Edit comment.
	import { onMount, tick } from 'svelte';

	let {
		initial,
		oncommit,
		oncancel
	}: {
		initial: string;
		oncommit: (value: string) => Promise<void>;
		oncancel: () => void;
	} = $props();

	let draft = $state('');
	let inputEl = $state<HTMLInputElement | null>(null);
	// Enter then the blur it causes must save once; Escape then its blur must not save.
	let settled = false;

	onMount(() => {
		void (async () => {
			draft = initial;
			await tick();
			inputEl?.focus();
			inputEl?.select();
		})();
	});

	function commit(): void {
		if (settled) return;
		settled = true;
		if (draft === initial) {
			oncancel();
			return;
		}
		void oncommit(draft);
	}

	function cancel(): void {
		if (settled) return;
		settled = true;
		oncancel();
	}

	function onKeydown(event: KeyboardEvent): void {
		// The table's own keys (arrows, Enter to load, Space) never see the editor's.
		event.stopPropagation();
		if (event.key === 'Enter') {
			event.preventDefault();
			commit();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancel();
		}
	}
</script>

<input
	bind:this={inputEl}
	bind:value={draft}
	class="comment-cell-editor"
	type="text"
	aria-label="Edit comment"
	data-testid="comment-cell-editor"
	spellcheck="false"
	onkeydown={onKeydown}
	onblur={commit}
	onclick={(e) => e.stopPropagation()}
	ondblclick={(e) => e.stopPropagation()}
	onpointerdown={(e) => e.stopPropagation()}
	ondragstart={(e) => {
		e.preventDefault();
		e.stopPropagation();
	}}
/>

<style>
	.comment-cell-editor {
		box-sizing: border-box;
		width: 100%;
		height: 100%;
		min-height: 18px;
		margin: 0;
		padding: 0 4px;
		font: inherit;
		color: var(--rb-text);
		background: var(--rb-panel);
		border: 1px solid var(--rb-accent);
		border-radius: 2px;
		outline: none;
	}

	.comment-cell-editor:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
</style>
