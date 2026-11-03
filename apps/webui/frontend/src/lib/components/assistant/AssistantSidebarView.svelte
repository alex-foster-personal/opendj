<script lang="ts">
	/**
	 * Props-only half of AssistantSidebar: the setup assistant's chat panel,
	 * its unavailable note, and the hidden marker agents read when no key is
	 * configured. The container probes status and streams replies; this file
	 * draws them. The draft and the log element are bindable.
	 */
	import type { AssistantPanelMode, AssistantStatus, ChatMessage } from '$lib/assistant/assistant-api';
	import { AGENT_DETAILS_LABEL, humanAssistantStatusError } from '$lib/setup/present';

	let {
		visible,
		mode,
		status,
		statusError,
		messages,
		streaming,
		sendError,
		canSend,
		draft = $bindable(),
		log = $bindable(),
		send,
		onStop
	}: {
		visible: boolean;
		mode: AssistantPanelMode;
		status: AssistantStatus | null;
		statusError: string | null;
		messages: ChatMessage[];
		/** The reply being streamed right now, or null between turns. */
		streaming: string | null;
		sendError: string | null;
		canSend: boolean;
		draft: string;
		log?: HTMLDivElement | null;
		send: () => Promise<void>;
		/** Abort the reply in flight. */
		onStop: () => void;
	} = $props();

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			void send();
		}
	}
</script>

{#if mode === 'chat'}
	<aside class="assistant-sidebar" aria-label="Setup assistant">
		<header>
			<h2>Assistant</h2>
			{#if status}
				<span class="model" title="The model answering these questions">
					Assistant ready
				</span>
			{/if}
		</header>

		<div class="log" role="log" aria-live="polite" aria-label="Conversation" bind:this={log}>
			{#if messages.length === 0 && streaming === null}
				<p class="muted intro">
					Your library is importing. Ask anything about it, or about DJ libraries in
					general, while you wait.
				</p>
			{/if}
			{#each messages as message, index (index)}
				<p class="bubble {message.role}">{message.content}</p>
			{/each}
			{#if streaming !== null}
				<p class="bubble assistant pending">
					{streaming}<span class="caret" aria-hidden="true"></span>
				</p>
			{/if}
		</div>

		{#if sendError !== null}
			<p class="notice danger" role="alert">{sendError}</p>
		{/if}

		<form
			class="composer"
			onsubmit={(event) => {
				event.preventDefault();
				void send();
			}}
		>
			<textarea
				bind:value={draft}
				onkeydown={onKeydown}
				rows="2"
				placeholder="Ask about your library..."
				aria-label="Message the assistant"
			></textarea>
			<div class="composer-row">
				<span
					class="counter"
					title="Characters streamed back so far in the reply being written."
				>
					{streaming !== null ? `${streaming.length} chars` : ''}
				</span>
				{#if streaming !== null}
					<button type="button" class="ghost" onclick={() => onStop()}>
						Stop
					</button>
				{:else}
					<button type="submit" disabled={!canSend}>Send</button>
				{/if}
			</div>
		</form>
	</aside>
{:else if mode === 'unavailable'}
	<aside class="assistant-unavailable" aria-label="Assistant unavailable">
		<p class="muted status-note" role="status">{humanAssistantStatusError()}</p>
		<details class="agent-details-only">
			<summary>{AGENT_DETAILS_LABEL}</summary>
			<pre data-agent-assistant-error={statusError}>{statusError}</pre>
		</details>
	</aside>
{:else if visible && status !== null && !status.configured}
	<!-- Unconfigured: no operator panel. Status remains on GET /api/v1/assistant/status. -->
	<div hidden data-agent-assistant-configured="false"></div>
{/if}

<style>
	.assistant-sidebar {
		display: flex;
		flex-direction: column;
		min-width: 18rem;
		max-width: 24rem;
		min-height: 0;
		border-left: 1px solid var(--border);
		padding: 0.8rem;
		gap: 0.6rem;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	h2 {
		margin: 0;
		font-size: 0.95rem;
	}
	.model {
		color: var(--muted);
		font-size: 0.7rem;
		cursor: help;
	}
	.notice {
		margin: 0;
		font-size: 0.8rem;
		line-height: 1.45;
	}
	.notice.danger {
		color: var(--danger);
		border: 1px solid var(--danger);
		border-radius: 4px;
		padding: 0.5rem 0.6rem;
	}
	.muted {
		color: var(--muted);
	}
	.assistant-unavailable {
		min-width: 12rem;
		max-width: 24rem;
		border-left: 1px solid var(--border);
		padding: 0.8rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.status-note {
		margin: 0;
		font-size: 0.8rem;
		line-height: 1.45;
	}
	.agent-details-only {
		font-size: 0.75rem;
		color: var(--muted);
	}
	.agent-details-only pre {
		white-space: pre-wrap;
		margin: 0.35rem 0 0;
		font-size: 0.72rem;
	}
	.log {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
	}
	.intro {
		font-size: 0.8rem;
		line-height: 1.45;
		margin: 0;
	}
	.bubble {
		margin: 0;
		padding: 0.45rem 0.6rem;
		border-radius: 6px;
		font-size: 0.82rem;
		line-height: 1.45;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.bubble.user {
		background: var(--surface);
		align-self: flex-end;
		max-width: 90%;
	}
	.bubble.assistant {
		border: 1px solid var(--border);
	}
	.bubble.pending {
		color: var(--muted);
	}
	.caret {
		display: inline-block;
		width: 0.45em;
		height: 1em;
		margin-left: 1px;
		vertical-align: text-bottom;
		background: var(--accent);
		animation: blink 1s step-end infinite;
	}
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.caret {
			animation: none;
		}
	}
	.composer {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	textarea {
		width: 100%;
		resize: vertical;
		background: var(--surface);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0.4rem 0.5rem;
		font: inherit;
		font-size: 0.82rem;
	}
	textarea:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}
	.composer-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.counter {
		color: var(--muted);
		font-size: 0.7rem;
		font-variant-numeric: tabular-nums;
		cursor: help;
	}
	button {
		font: inherit;
		font-size: 0.78rem;
		padding: 0.3rem 0.7rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--fg);
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	button.ghost {
		color: var(--muted);
	}
</style>
