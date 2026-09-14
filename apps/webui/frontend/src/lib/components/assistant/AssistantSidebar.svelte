<script lang="ts">
	/**
	 * Assistant chat sidebar shown while setup runs / the library populates.
	 *
	 * CONTRACT (integration point between the setup-overlay work and the
	 * assistant-backend work -- keep the export shape stable):
	 *   props: { visible: boolean }
	 *
	 * Backend: GET /api/v1/assistant/status + POST /api/v1/assistant/chat,
	 * an OpenRouter proxy (apps/engine_core/assistant/api.py). Every path,
	 * type and refusal code lives in $lib/assistant/assistant-api.
	 *
	 * The house rule this component exists under: an assistant that cannot
	 * reach a model renders a notice, never an input box. A text field that
	 * accepts a question and answers nothing is the UI equivalent of mocked
	 * data, and it is worse here than anywhere else in the app because the
	 * user cannot tell a broken key from a model with nothing to say.
	 */
	import {
		AssistantError,
		getAssistantStatus,
		streamChat,
		type AssistantStatus,
		type ChatMessage
	} from '$lib/assistant/assistant-api';

	let { visible }: { visible: boolean } = $props();

	let status = $state<AssistantStatus | null>(null);
	let statusError = $state<string | null>(null);
	let messages = $state<ChatMessage[]>([]);
	let draft = $state('');
	/** The reply being streamed right now, or null between turns. Held apart
	 * from `messages` so a half-arrived answer is visibly in progress and is
	 * never mistaken for a finished one. */
	let streaming = $state<string | null>(null);
	let sendError = $state<string | null>(null);
	let controller: AbortController | null = null;
	let log: HTMLDivElement | null = $state(null);

	const canSend = $derived(
		status?.configured === true && streaming === null && draft.trim().length > 0
	);

	/** Ask what the engine can do, the first time the sidebar is shown.
	 *
	 * Deliberately not on mount: the sidebar is mounted alongside the wizard
	 * and may never be opened, and a status request for a panel nobody looked
	 * at is a request nobody asked for.
	 */
	$effect(() => {
		if (!visible || status !== null || statusError !== null) return;
		const probe = new AbortController();
		getAssistantStatus(probe.signal)
			.then((value) => {
				status = value;
			})
			.catch((error: unknown) => {
				if (probe.signal.aborted) return;
				statusError =
					error instanceof AssistantError || error instanceof Error
						? error.message
						: String(error);
			});
		return () => probe.abort();
	});

	/** Abort any in-flight reply when the sidebar goes away. */
	$effect(() => () => controller?.abort());

	function scrollToLatest(): void {
		// After the DOM has the new text, not before, or this scrolls to where
		// the log used to end.
		requestAnimationFrame(() => {
			if (log) log.scrollTop = log.scrollHeight;
		});
	}

	async function send(): Promise<void> {
		const question = draft.trim();
		if (!canSend) return;
		draft = '';
		sendError = null;
		messages = [...messages, { role: 'user', content: question }];
		streaming = '';
		scrollToLatest();

		controller = new AbortController();
		try {
			for await (const chunk of streamChat(messages, controller.signal)) {
				streaming = (streaming ?? '') + chunk;
				scrollToLatest();
			}
			// An empty stream is a failure, not an answer. Saying so beats
			// appending a blank bubble the user has to interpret.
			if (streaming.length === 0) {
				sendError = 'the model returned an empty reply';
			} else {
				messages = [...messages, { role: 'assistant', content: streaming }];
			}
		} catch (error: unknown) {
			if (controller.signal.aborted) return;
			sendError =
				error instanceof AssistantError || error instanceof Error
					? error.message
					: String(error);
		} finally {
			streaming = null;
			controller = null;
			scrollToLatest();
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		// Enter sends, Shift+Enter breaks the line -- the convention every
		// chat input in the world already taught the user.
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			void send();
		}
	}
</script>

{#if visible}
	<aside class="assistant-sidebar" aria-label="Setup assistant">
		<header>
			<h2>Assistant</h2>
			{#if status}
				<span class="model" title="The OpenRouter model answering these questions. Override it with MDT_ASSISTANT_MODEL.">
					{status.model}
				</span>
			{/if}
		</header>

		{#if statusError !== null}
			<p class="notice danger" role="alert">
				The assistant could not be reached: {statusError}
			</p>
		{:else if status === null}
			<p class="notice muted">Checking whether the assistant is configured...</p>
		{:else if !status.configured}
			<p class="notice muted">
				The assistant is not set up yet. You can finish setup without it.
			</p>
		{:else}
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
						title="Characters streamed back so far in the reply being written. Characters, not tokens: a plain-text stream does not carry a token count, and showing an invented one would be a made-up number."
					>
						{streaming !== null ? `${streaming.length} chars` : ''}
					</span>
					{#if streaming !== null}
						<button type="button" class="ghost" onclick={() => controller?.abort()}>
							Stop
						</button>
					{:else}
						<button type="submit" disabled={!canSend}>Send</button>
					{/if}
				</div>
			</form>
		{/if}
	</aside>
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
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
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
	.notice.muted,
	.muted {
		color: var(--muted);
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
	/* The reply is still arriving. Dimmed so a half-written answer never
	   reads as a finished one. */
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
