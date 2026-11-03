<script lang="ts">
	/**
	 * The live agent sidebar: owns the NDJSON stream (agent-api.ts) and the
	 * session fold (agent-session.ts) and hands both to AgentSidebarView.
	 *
	 * Events are folded once per animation frame, never per delta, so a
	 * streaming reply costs the /performance waveform at most one Svelte
	 * update a frame.
	 */
	import type { AgentEvent, ContextRef } from '$lib/assistant/agent-contract';
	import { streamAgent } from '$lib/assistant/agent-api';
	import { availabilityOf, contextChip, contextChipId } from '$lib/assistant/agent-labels';
	import {
		EMPTY_SESSION,
		applyEvent,
		endTurn,
		foldEvents,
		heldCalls,
		requestFor,
		resumeTurn,
		startTurn,
		type Session
	} from '$lib/assistant/agent-session';
	import { AssistantError, previewInstant } from '$lib/assistant/assistant-api';
	import { createAssistantStatusProbe } from '$lib/assistant/status-probe.svelte';

	import AgentSidebarView, { type Decision, type TierChoice } from './AgentSidebarView.svelte';

	let {
		open = true,
		onclose,
		contexts = $bindable([])
	}: { open?: boolean; onclose: () => void; contexts?: ContextRef[] } = $props();

	const SUGGESTIONS = [
		'Find techno around 128 BPM I have not played lately',
		'What is loaded on each deck?',
		'Turn the system volume down a bit'
	];
	/** Typing pause before a keyless draft is checked against the engine's instant rules. */
	const PREVIEW_DEBOUNCE_MS = 150;

	let session = $state.raw<Session>(EMPTY_SESSION);
	let decisions = $state.raw<Record<string, Decision>>({});
	const statusProbe = createAssistantStatusProbe(() => open);
	let draft = $state('');
	let tierChoice = $state<TierChoice>('auto');

	let controller: AbortController | null = null;
	let queue: AgentEvent[] = [];
	let frame = 0;

	const chips = $derived(contexts.map(contextChip));
	const availability = $derived(availabilityOf(statusProbe.status, statusProbe.statusError));
	/** Keyless only: whether the draft is an instant command; null while the engine is asked. */
	let instantDraft = $state<boolean | null>(null);
	/** Why the last draft check failed. About this draft only: never availability, and the next edit clears it. */
	let instantError = $state<string | null>(null);

	$effect(() => {
		const text = draft.trim();
		instantDraft = null;
		instantError = null;
		if (availability !== 'keyless' || !text) return;
		const probe = new AbortController();
		const timer = setTimeout(() => {
			previewInstant(text, probe.signal).then(
				(value) => (instantDraft = value),
				(error: unknown) => {
					if (!probe.signal.aborted) instantError = messageOf(error);
				}
			);
		}, PREVIEW_DEBOUNCE_MS);
		return () => {
			clearTimeout(timer);
			probe.abort();
		};
	});

	$effect(() => () => {
		controller?.abort();
		cancelAnimationFrame(frame);
	});

	//----- stream

	async function run(next: Session, approved: string[] = []): Promise<void> {
		session = next;
		const own = (controller = new AbortController());
		const options = { tier: tierChoice === 'auto' ? null : tierChoice, context: contexts };
		try {
			for await (const event of streamAgent(requestFor(next, approved, options), own.signal)) enqueue(event);
		} catch (error: unknown) {
			if (!own.signal.aborted) {
				flush();
				session = applyEvent(session, { type: 'error', code: codeOf(error), message: messageOf(error) });
			}
		} finally {
			if (controller === own) {
				flush();
				session = endTurn(session);
				controller = null;
			}
		}
	}

	function enqueue(event: AgentEvent): void {
		queue.push(event);
		frame ||= requestAnimationFrame(flush);
	}

	function flush(): void {
		cancelAnimationFrame(frame);
		frame = 0;
		if (queue.length === 0) return;
		session = foldEvents(session, queue);
		queue = [];
	}

	//----- view callbacks

	function send(text: string): void {
		if (session.busy) return;
		decisions = {};
		void run(startTurn(session, text));
	}

	/** Resume only once every held call has an answer; unapproved ones are declined by the engine. */
	function decide(callId: string, decision: Decision): void {
		decisions = { ...decisions, [callId]: decision };
		const held = heldCalls(session);
		if (!held.every((call) => decisions[call.id])) return;
		const approved = held.filter((call) => decisions[call.id] === 'approve').map((call) => call.id);
		void run(resumeTurn(session), approved);
	}

	function newChat(): void {
		controller?.abort();
		controller = null;
		queue = [];
		cancelAnimationFrame(frame);
		frame = 0;
		session = EMPTY_SESSION;
		decisions = {};
		draft = '';
	}

	function removeContext(id: string): void {
		contexts = contexts.filter((ref) => contextChipId(ref) !== id);
	}

	//----- errors

	function codeOf(error: unknown): string {
		return error instanceof AssistantError ? error.code : 'assistant_client_error';
	}

	function messageOf(error: unknown): string {
		return error instanceof Error ? error.message : String(error);
	}
</script>

<AgentSidebarView
	items={session.items}
	busy={session.busy}
	done={session.done}
	{availability}
	{open}
	{instantDraft}
	{instantError}
	mode={statusProbe.status?.mode ?? null}
	bind:draft
	suggestions={SUGGESTIONS}
	{decisions}
	contexts={chips}
	{tierChoice}
	ontier={(choice) => (tierChoice = choice)}
	onremovecontext={removeContext}
	onsend={send}
	onapprove={(id) => decide(id, 'approve')}
	ondecline={(id) => decide(id, 'decline')}
	onstop={() => controller?.abort()}
	onnewchat={newChat}
	{onclose}
/>
