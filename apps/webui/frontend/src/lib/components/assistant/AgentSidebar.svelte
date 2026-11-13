<script lang="ts">
	/**
	 * The live agent sidebar: owns the NDJSON stream (agent-api.ts) and the
	 * session fold (agent-session.ts) and hands both to AgentSidebarView.
	 *
	 * Events are folded once per animation frame, never per delta, so a
	 * streaming reply costs the /performance waveform at most one Svelte
	 * update a frame.
	 *
	 * OJ (#13711): the header glass thinks while a turn runs and smiles when the reply lands;
	 * the panel fills with juice meanwhile. ojMotionPolicy holds it static while a deck plays
	 * on /performance and under reduced motion; the loading variant can turn the fill off.
	 * Mood (#13937): each folded batch of events may start one beat of smile, slosh or bubbles
	 * (oj-agent-mood.ts); one timer ends the beat, and the mood variant can keep it calm.
	 * Starters (#14040): an empty log shows route-keyed chips (oj-starters.ts); a chip fills the
	 * draft and never sends.
	 */
	import type { AgentEvent, ContextRef } from '$lib/assistant/agent-contract';
	import { streamAgent } from '$lib/assistant/agent-api';
	import { availabilityOf, contextChip, contextChipId } from '$lib/assistant/agent-labels';
	import {
		OJ_MOOD_BEAT_MS,
		OJ_MOOD_VARIANT,
		ojMoodBeat,
		ojMoodLook,
		ojMoodStyle,
		type OjMoodBeat
	} from '$lib/assistant/oj-agent-mood';
	import {
		EMPTY_SESSION,
		endTurn,
		foldEvents,
		heldCalls,
		requestFor,
		resumeTurn,
		startTurn,
		type Session
	} from '$lib/assistant/agent-session';
	import { AssistantError, previewInstant } from '$lib/assistant/assistant-api';
	import { ojStartersFor } from '$lib/assistant/oj-starters';
	import { createAssistantStatusProbe } from '$lib/assistant/status-probe.svelte';
	import { prefersReducedMotion, watchReducedMotionMedia } from '$lib/motion/reduced-motion';
	import { anyDeckPlaying } from '$lib/rb/playing-gate';
	import { readVariant } from '$lib/rb/prefs.svelte';
	import { OJ_LOADING_VARIANT, ojAgentLook, ojLoadingStyle } from './oj-agent-header';
	import { ojMotionPolicy } from './oj-motion-policy';

	import AgentSidebarView, { type Decision, type TierChoice } from './AgentSidebarView.svelte';
	import OjStarterChips from './OjStarterChips.svelte';

	let {
		open = true,
		onclose,
		contexts = $bindable([])
	}: { open?: boolean; onclose: () => void; contexts?: ContextRef[] } = $props();

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

	/** Bumped when the OS reduced-motion query flips; a hidden document counts as reduced. */
	let motionTick = $state(0);
	let documentHidden = $state(false);
	const ojMotion = $derived.by(() => {
		void motionTick;
		return ojMotionPolicy({
			reducedMotion: prefersReducedMotion() || documentHidden,
			anyDeckPlaying: anyDeckPlaying(),
			performanceVisible:
				typeof location !== 'undefined' && location.pathname.startsWith('/performance'),
			streaming: session.busy
		});
	});
	const ojMood = $derived(ojMoodStyle(readVariant(OJ_MOOD_VARIANT.id)));
	/** The live mood beat and the clock it is judged against; the beat timer advances the clock. */
	let moodBeat = $state.raw<OjMoodBeat | null>(null);
	let moodClock = $state(0);
	let moodTimer: ReturnType<typeof setTimeout> | undefined;
	const ojLook = $derived(
		ojMoodLook(
			ojAgentLook(session, ojMotion, ojLoadingStyle(readVariant(OJ_LOADING_VARIANT.id))),
			moodBeat,
			moodClock,
			ojMood
		)
	);

	$effect(() => {
		const onVisibility = () => (documentHidden = document.visibilityState === 'hidden');
		onVisibility();
		const stopWatching = watchReducedMotionMedia(() => (motionTick += 1));
		document.addEventListener('visibilitychange', onVisibility);
		return () => {
			stopWatching();
			document.removeEventListener('visibilitychange', onVisibility);
		};
	});

	/** The panel stays mounted across routes, so each open re-reads the route for the starter list. */
	const starters = $derived.by(() => {
		void open;
		return ojStartersFor(typeof location === 'undefined' ? '' : location.pathname, session.items);
	});

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
		clearTimeout(moodTimer);
	});

	//----- stream

	async function run(next: Session, approved: string[] = []): Promise<void> {
		session = next;
		const own = (controller = new AbortController());
		const options = { tier: tierChoice === 'auto' ? null : tierChoice, context: contexts };
		try {
			for await (const event of streamAgent(requestFor(next, approved, options), own.signal))
				enqueue(event);
		} catch (error: unknown) {
			if (!own.signal.aborted) {
				flush();
				fold([{ type: 'error', code: codeOf(error), message: messageOf(error) }]);
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
		const events = queue;
		queue = [];
		fold(events);
	}

	function fold(events: AgentEvent[]): void {
		session = foldEvents(session, events);
		react(events);
	}

	/** Start the beat these events earn, if any; a newer beat replaces a running one. */
	function react(events: AgentEvent[]): void {
		const now = Date.now();
		const beat = ojMoodBeat(events, { items: session.items, motion: ojMotion, style: ojMood, now });
		if (beat === null) return;
		clearTimeout(moodTimer);
		moodBeat = beat;
		moodClock = now;
		moodTimer = setTimeout(() => (moodClock = beat.endsAt), OJ_MOOD_BEAT_MS);
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
		clearTimeout(moodTimer);
		moodBeat = null;
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

{#snippet starterChips()}
	<OjStarterChips {starters} motion={ojMotion} onpick={(text) => (draft = text)} />
{/snippet}

<AgentSidebarView
	items={session.items}
	busy={session.busy}
	done={session.done}
	{availability}
	{open}
	{instantDraft}
	{instantError}
	mode={statusProbe.status?.mode ?? null}
	oj={ojLook}
	bind:draft
	starters={starterChips}
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
