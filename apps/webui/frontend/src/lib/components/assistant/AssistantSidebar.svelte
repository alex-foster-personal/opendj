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
	 * When no key is configured the panel is hidden entirely for operators;
	 * agents still read GET /api/v1/assistant/status.
	 */
	import {
		AssistantError,
		assistantPanelMode,
		getAssistantStatus,
		streamChat,
		type AssistantStatus,
		type ChatMessage
	} from '$lib/assistant/assistant-api';
	import AssistantSidebarView from './AssistantSidebarView.svelte';
	import type { OjFillMode, OjFillPhase } from './oj-fill';

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
	let fillPhase = $state<OjFillPhase>('idle');
	// #9474 (O4 motion policy) will pass policy.fillMode here.
	const fillMode: OjFillMode = 'animated';

	const canSend = $derived(
		status?.configured === true && streaming === null && draft.trim().length > 0
	);
	const mode = $derived(assistantPanelMode(visible, status, statusError));

	/** Ask what the engine can do, the first time the sidebar is shown. */
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
					error instanceof AssistantError || error instanceof Error ? error.message : String(error);
			});
		return () => probe.abort();
	});

	/** Abort any in-flight reply when the sidebar goes away. */
	$effect(() => () => controller?.abort());

	function scrollToLatest(): void {
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
		// Only a landed reply drains; stop or error snap empty so drain always means the answer arrived.
		fillPhase = 'waiting';
		scrollToLatest();

		controller = new AbortController();
		try {
			for await (const chunk of streamChat(messages, controller.signal)) {
				streaming = (streaming ?? '') + chunk;
				if (fillPhase === 'waiting') fillPhase = 'streaming';
				scrollToLatest();
			}
			if (streaming.length === 0) {
				sendError = 'The assistant did not return a reply. Try again.';
				fillPhase = 'idle';
			} else {
				messages = [...messages, { role: 'assistant', content: streaming }];
				fillPhase = 'done';
			}
		} catch (error: unknown) {
			fillPhase = 'idle';
			if (controller.signal.aborted) return;
			sendError = 'The assistant could not answer right now. Try again.';
		} finally {
			streaming = null;
			controller = null;
			scrollToLatest();
		}
	}
</script>

<AssistantSidebarView
	{visible}
	{mode}
	{status}
	{statusError}
	{messages}
	{streaming}
	{sendError}
	{canSend}
	bind:draft
	bind:log
	{send}
	onStop={() => controller?.abort()}
	{fillPhase}
	{fillMode}
/>
