<!--
	The app-wide toast tray.

	Extracted from +layout.svelte for the same reason DeckErrorBanner was
	extracted from Deck.svelte: +layout.svelte is a hotspot file several agents
	touch at once, and the toast behavior belongs somewhere its own tests can
	name it.

	THREE BEHAVIORS, all of which exist because a toast that reported a real
	failure could not be read in time:

	  HOVER HOLDS. A pointer over a toast clears its dismissal timer, and
	    leaving restarts the FULL delay rather than the remainder. Reading a
	    message must not cost you the message.
	  X DISMISSES. The autoplay error toast had no way out and sat there.
	  CLICK COPIES. The body is a button that puts the message, its id, its
	    timestamp and the environment on the clipboard, so reporting it is one
	    click rather than a retype from a screenshot.

	The X is nested inside the clickable body, so its click must stopPropagation
	or dismissing would also copy.
-->
<script lang="ts">
	import { copyToast, dismissToast, holdToast, releaseToast, type Toast } from '$lib/stores.svelte';

	// Presentational: the store owns the toast list and every action. Kept off
	// $lib/rb/types.ts entirely (that module is at its fan-in ceiling), which
	// costs nothing here because a toast has no deck identity to type.
	let { items }: { items: readonly Toast[] } = $props();

	/** Per-toast transient copy feedback, keyed by correlation id. */
	let copyState = $state<Record<string, 'copied' | string>>({});

	async function copy(toast: Toast): Promise<void> {
		try {
			await copyToast(toast.logId);
			copyState[toast.logId] = 'copied';
			setTimeout(() => {
				delete copyState[toast.logId];
			}, 1500);
		} catch (exc) {
			// Visible failure, per the repo's fail-fast rule: a copy that did
			// nothing must never look like a copy that worked.
			copyState[toast.logId] = exc instanceof Error ? exc.message : String(exc);
		}
	}
</script>

<div class="toast-stack">
	{#each items as toast (toast.id)}
		<div
			class="toast"
			class:error={toast.kind === 'error'}
			role="status"
			data-toast-id={toast.logId}
			onmouseenter={() => holdToast(toast.logId)}
			onmouseleave={() => releaseToast(toast.logId)}
		>
			<button
				type="button"
				class="toast-body"
				data-toast-copy={toast.logId}
				title={`Click to copy this message with its id (${toast.logId}), timestamp and environment. Hovering keeps it on screen.`}
				onclick={() => copy(toast)}
			>
				<span class="toast-message">{toast.message}</span>
				{#if toast.count > 1}
					<span class="toast-note" data-toast-count={toast.count}>Repeated {toast.count} times</span>
				{/if}
				{#if copyState[toast.logId] === 'copied'}
					<span class="toast-note" data-toast-copied={toast.logId}>copied</span>
				{:else if copyState[toast.logId] !== undefined}
					<span class="toast-note failed" data-toast-copy-failed={toast.logId}
						>{copyState[toast.logId]}</span
					>
				{/if}
			</button>
			<button
				type="button"
				class="toast-dismiss"
				data-toast-dismiss={toast.logId}
				title="Dismiss this message"
				aria-label={`Dismiss message ${toast.logId}`}
				onclick={(event) => {
					event.stopPropagation();
					dismissToast(toast.logId);
				}}>x</button
			>
		</div>
	{/each}
</div>

<style>
	/* Layout only. The .toast-stack / .toast palette stays in app.css, where it
	   was, so an existing toast keeps looking like an existing toast. */
	.toast {
		display: flex;
		align-items: flex-start;
		gap: 0.4rem;
	}
	.toast-body {
		flex: 1 1 auto;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 0;
		cursor: pointer;
	}
	.toast-body:hover .toast-message {
		text-decoration: underline dotted;
	}
	.toast-note {
		font-size: 0.75em;
		opacity: 0.8;
	}
	.toast-note.failed {
		color: var(--danger);
		opacity: 1;
	}
	.toast-dismiss {
		flex: 0 0 auto;
		border: 1px solid var(--border);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		line-height: 1.1;
		padding: 0 0.3rem;
		cursor: pointer;
	}
	.toast-dismiss:hover {
		background: var(--danger);
		color: #fff;
	}
</style>
