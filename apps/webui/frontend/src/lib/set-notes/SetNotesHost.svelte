<script lang="ts">
	/**
	 * SETNOTE-6340/6340b: owns the right-Cmd tap for as long as it is mounted (the
	 * root layout mounts it once, after boot) and draws the pad while open.
	 * The pad, and the ProseMirror editor inside it, is imported the first time
	 * the pad opens, so a session that never opens it never downloads it.
	 */
	import { onMount, type Component } from 'svelte';
	import { installSetNotesHotkey } from './hotkey/install-hotkey';
	import { padUi, setSetNotesOpen, toggleSetNotes } from './store.svelte';
	import { defineSetNotesToggle } from './toggle-element';

	let { onError }: { onError: (error: unknown) => void } = $props();
	let Pad = $state<Component | null>(null);
	let importing = false;

	onMount(() => {
		defineSetNotesToggle();
		return installSetNotesHotkey(toggleSetNotes);
	});

	$effect(() => {
		if (!padUi.open || Pad !== null || importing) return;
		importing = true;
		import('./SetNotesPad.svelte')
			.then((m) => (Pad = m.default))
			.catch((error: unknown) => {
				importing = false;
				// Closed so the next tap retries instead of toggling an invisible pad.
				setSetNotesOpen(false);
				onError(error);
			});
	});
</script>

{#if padUi.open && Pad}
	<Pad />
{/if}
