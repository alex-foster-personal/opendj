<script lang="ts">
	// R-TAG-4: removing a tag asks once, says ⌘Z undoes it, and can be snoozed
	// for an hour during a library session (D13). Enter removes; Esc cancels.
	import EditSuiteModal from '../EditSuiteModal.svelte';
	import {
		removeTagNow,
		snoozeTagRemovalConfirm,
		tagState,
		type TagRemovalRequest
	} from '$lib/rb/library-tags.svelte';

	let { request }: { request: TagRemovalRequest } = $props();
	let snooze = $state(false);

	function close(): void {
		tagState.removal = null;
	}

	function confirm(): void {
		// Read the prop first: closing clears the store it comes from.
		const removal = request;
		if (snooze) snoozeTagRemovalConfirm(Date.now());
		close();
		void removeTagNow(removal);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			confirm();
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<EditSuiteModal title="Remove tag" onclose={close}>
	<p>Remove #{request.name} from this track? ⌘Z undoes it.</p>
	<label>
		<input type="checkbox" bind:checked={snooze} data-testid="tag-remove-snooze" />
		Don't ask again for the next hour
	</label>
	<div class="actions">
		<button class="rb-lit-button" onclick={close}>Cancel</button>
		<button class="rb-lit-button primary" data-testid="tag-remove-yes" onclick={confirm}
			>Remove</button
		>
	</div>
</EditSuiteModal>

<style>
	p {
		margin: 0 0 10px;
		font-size: 12px;
	}
	label {
		display: flex;
		gap: 6px;
		align-items: center;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 6px;
		margin-top: 12px;
	}
</style>
