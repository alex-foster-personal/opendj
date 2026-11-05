<script lang="ts">
	/**
	 * The "Ask AI to change setting for you" block at the foot of the settings
	 * results: AI search status, the ask button, the outcome line and the
	 * Apply / Reject confirm for a proposed change. SettingsOverlay.svelte owns
	 * the request and the apply; this renders its state.
	 */
	interface Proposal {
		key: string;
		value: boolean | string;
		rationale: string;
	}

	interface Props {
		aiPending: boolean;
		aiError: string | null;
		applyBusy: boolean;
		/** The settings search query; asking needs at least two characters. */
		query: string;
		applyMsg: string | null;
		applyOk: boolean | null;
		pendingProposal: Proposal | null;
		askAiApply: () => void;
		confirmProposal: (accept: boolean) => void;
	}

	let {
		aiPending,
		aiError,
		applyBusy,
		query,
		applyMsg,
		applyOk,
		pendingProposal,
		askAiApply,
		confirmProposal
	}: Props = $props();
</script>

{#if aiPending}
	<p class="so-ai-status">AI matching...</p>
{:else if aiError}
	<p class="so-ai-status err" title={aiError}>AI search unavailable</p>
{/if}

<div class="so-ai-ask">
	<button
		type="button"
		class="so-ask-btn"
		disabled={applyBusy || query.trim().length < 2}
		onclick={() => askAiApply()}
		title="Ask the fast model to propose one allowlisted setting change"
	>
		{applyBusy ? 'Asking AI...' : 'Ask AI to change setting for you'}
	</button>
	{#if applyMsg}
		<p class="so-apply-msg" class:ok={applyOk === true} class:bad={applyOk === false}>
			{#if applyOk === true}<svg
					class="so-icon"
					width="10"
					height="10"
					viewBox="0 0 8 8"
					aria-hidden="true"
					><path
						d="M1 4.2 3.1 6.3 7 1.8"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					/></svg
				>{:else if applyOk === false}<svg
					class="so-icon"
					width="10"
					height="10"
					viewBox="0 0 8 8"
					aria-hidden="true"
					><path
						d="M1.6 1.6 6.4 6.4M6.4 1.6 1.6 6.4"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
					/></svg
				>{/if}
			{applyMsg}
		</p>
	{/if}
	{#if pendingProposal}
		<div class="so-confirm">
			<button type="button" onclick={() => confirmProposal(true)}
				><svg class="so-icon" width="10" height="10" viewBox="0 0 8 8" aria-hidden="true"
					><path
						d="M1 4.2 3.1 6.3 7 1.8"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					/></svg
				> Apply</button
			>
			<button type="button" onclick={() => confirmProposal(false)}
				><svg class="so-icon" width="10" height="10" viewBox="0 0 8 8" aria-hidden="true"
					><path
						d="M1.6 1.6 6.4 6.4M6.4 1.6 1.6 6.4"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
					/></svg
				> Reject</button
			>
		</div>
	{/if}
</div>

<style>
	.so-ai-status {
		color: var(--muted, #9aa4b2);
		font-size: 0.85rem;
		margin: 8px 4px;
	}
	.so-ai-status.err {
		color: var(--danger, #ff5a5a);
	}
	.so-ai-ask {
		margin-top: 12px;
		padding-top: 10px;
		border-top: 1px solid var(--border, #1c222c);
	}
	.so-ask-btn {
		width: 100%;
		padding: 10px 12px;
		border-radius: 10px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface-hover, #1a212c);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font-size: 0.9rem;
	}
	.so-ask-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.so-icon {
		vertical-align: -1px;
	}
	.so-apply-msg {
		margin: 8px 2px 0;
		font-size: 0.82rem;
		color: var(--muted, #9aa4b2);
	}
	.so-apply-msg.ok {
		color: #5dce8a;
	}
	.so-apply-msg.bad {
		color: var(--danger, #ff5a5a);
	}
	.so-confirm {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
	.so-confirm button {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
	}
</style>
