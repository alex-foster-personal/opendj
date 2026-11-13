<script lang="ts" module>
	export type TierChoice = 'auto' | 'fast' | 'think';
	export type Decision = 'approve' | 'decline';
	export interface ContextChip {
		id: string;
		label: string;
		title: string;
	}
</script>

<script lang="ts">
	/**
	 * The agent sidebar, props in and callbacks out. AgentSidebar.svelte owns
	 * the stream; this file only draws a Session (agent-session.ts), so the
	 * live panel and its Storybook states render through the same markup.
	 */
	import { type DoneReason } from '$lib/assistant/agent-contract';
	import type { Item } from '$lib/assistant/agent-session';
	import type { AssistantStatus } from '$lib/assistant/assistant-api';
	import {
		KEYLESS_SEND_TITLE,
		QUEUED_SEND_NOTE,
		composerEscapeAction,
		heldKeyAction,
		instantCheckFailedNote,
		sendOutcome,
		type Availability
	} from '$lib/assistant/agent-labels';
	import { ASSISTANT_ROOT_ATTR } from '$lib/assistant/sidebar-toggle';
	import OjAgentHeader from './OjAgentHeader.svelte';
	import { OJ_AGENT_IDLE, type OjAgentLook } from './oj-agent-header';
	import { untrack, type Snippet } from 'svelte';
	import AgentSidebarComposer from './AgentSidebarComposer.svelte';
	import AgentSidebarConversationLog from './AgentSidebarConversationLog.svelte';

	interface Props {
		items: Item[];
		busy: boolean;
		done: DoneReason | null;
		/** 'ready' and 'keyless' open the composer; keyless sends instant commands only. */
		availability: Availability;
		/** False while the mount keeps the sidebar hidden; each open focuses the composer. */
		open?: boolean;
		/** Keyless only: the engine's verdict on the draft, null while it is asked. */
		instantDraft?: boolean | null;
		/** Keyless only: why the engine could not check the draft; the composer stays open. */
		instantError?: string | null;
		/** Who answers. Only the capped gateway lane is free; a user's own key bills that user. */
		mode?: AssistantStatus['mode'];
		/** OJ's glass and juice fill, decided by AgentSidebar.svelte (#13711). */
		oj?: OjAgentLook;
		draft?: string;
		suggestions?: string[];
		/** OJ starter chips (#14040), drawn under the intro while the log is empty; AgentSidebar.svelte decides. */
		starters?: Snippet;
		/** Held call id -> the user's answer, kept until the resumed turn settles it. */
		decisions?: Record<string, Decision>;
		contexts?: ContextChip[];
		tierChoice?: TierChoice;
		ontier: (choice: TierChoice) => void;
		onremovecontext?: (id: string) => void;
		onsend: (text: string) => void;
		onapprove: (callId: string) => void;
		ondecline: (callId: string) => void;
		onstop: () => void;
		onnewchat: () => void;
		onclose: () => void;
	}

	let {
		items,
		busy,
		done,
		availability,
		open: shown = true,
		instantDraft = null,
		instantError = null,
		mode = null,
		oj = OJ_AGENT_IDLE,
		draft = $bindable(''),
		suggestions = [],
		starters,
		decisions = {},
		contexts = [],
		tierChoice = 'auto',
		ontier,
		onremovecontext,
		onsend,
		onapprove,
		ondecline,
		onstop,
		onnewchat,
		onclose
	}: Props = $props();

	let toolOpen = $state<Record<string, boolean>>({});
	let root: HTMLElement | undefined = $state();
	let log: HTMLDivElement | undefined = $state();
	let composer: HTMLTextAreaElement | undefined = $state();
	let pinned = true;

	const held = $derived(items.some((item) => item.kind === 'tool' && item.status === 'held'));
	const canAnswerHeld = $derived(!busy && done === 'needs_confirmation');
	const nextHeld = $derived(
		canAnswerHeld
			? items.find((item) => item.kind === 'tool' && item.status === 'held' && !decisions[item.call.id])
			: undefined
	);
	const composerOpen = $derived(availability === 'ready' || availability === 'keyless');
	const outcome = $derived(
		sendOutcome({
			draft,
			composerOpen,
			busy,
			held,
			keyless: availability === 'keyless',
			instantDraft,
			instantCheckFailed: instantError !== null
		})
	);
	const instantCheckNote = $derived(instantError === null ? null : instantCheckFailedNote(instantError));
	/** What the last Enter or Send got short of sending, for the draft it got it for; editing makes it stale. */
	let attempt = $state<{ draft: string; outcome: 'queue' | 'refuse' } | null>(null);
	const attempted = $derived(attempt !== null && attempt.draft === draft ? attempt.outcome : null);
	const composerNote = $derived(
		instantCheckNote ?? (attempted === 'refuse' ? KEYLESS_SEND_TITLE : attempted === 'queue' ? QUEUED_SEND_NOTE : null)
	);
	const thinking = $derived(busy && items.at(-1)?.kind !== 'text');

	$effect(() => {
		void items;
		void busy;
		if (log && pinned) log.scrollTop = log.scrollHeight;
	});

	const nextHeldId = $derived(nextHeld?.kind === 'tool' ? nextHeld.call.id : null);

	/** Opening focuses the composer; a newly held call takes focus on its Do it, so Enter / Esc answer it. */
	$effect(() => {
		if (!shown || !root) return;
		if (nextHeldId !== null) {
			root.querySelector<HTMLButtonElement>(`[data-call-id="${CSS.escape(nextHeldId)}"] button.primary`)?.focus();
		} else if (composerOpen && !held) {
			composer?.focus();
		}
	});

	//----- input

	function send(): void {
		attempt = outcome === 'queue' || outcome === 'refuse' ? { draft, outcome } : null;
		if (outcome !== 'send') return;
		onsend(draft.trim());
		draft = '';
	}

	/** An edit discards the attempt outright, so restoring the old text never revives a queued Enter. */
	$effect(() => {
		if (attempt !== null && attempt.draft !== draft) attempt = null;
	});

	/** A queued Enter resolves by the same rule once the engine has answered. */
	$effect(() => {
		if (attempted === 'queue' && outcome !== 'queue') untrack(send);
	});

	function onComposerKey(event: KeyboardEvent): void {
		if (event.isComposing) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			if (composerEscapeAction({ draft, held }) === 'close') onclose();
			else composer?.blur();
			return;
		}
		if (event.key !== 'Enter' || event.shiftKey) return;
		event.preventDefault();
		send();
	}

	/** Enter approves and Esc declines the first unanswered held call. */
	function onWindowKey(event: KeyboardEvent): void {
		if (nextHeld?.kind !== 'tool') return;
		const target = event.target instanceof Element ? event.target : null;
		const action = heldKeyAction({
			key: event.key,
			modified: event.altKey || event.ctrlKey || event.metaKey || event.shiftKey,
			inSidebar: target !== null && root?.contains(target) === true,
			editing: target?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') != null,
			onButton: target?.closest('button, a') != null
		});
		if (!action) return;
		event.preventDefault();
		event.stopPropagation();
		(action === 'approve' ? onapprove : ondecline)(nextHeld.call.id);
	}

	function onLogScroll(): void {
		if (log) pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
	}

	function toggleToolOpen(id: string): void {
		toolOpen = { ...toolOpen, [id]: !toolOpen[id] };
	}
</script>

<svelte:window onkeydown={onWindowKey} />

<aside class="agent-sidebar" aria-label="OJ, your DJ assistant" bind:this={root} {...{ [ASSISTANT_ROOT_ATTR]: '' }}>
	<header>
		<OjAgentHeader look={oj} {mode} />
		<span class="grow"></span>
		<button type="button" class="icon" title="New chat" aria-label="New chat" onclick={onnewchat}>
			<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
		</button>
		<button type="button" class="icon" title="Close" aria-label="Close assistant" onclick={onclose}>
			<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
		</button>
	</header>

	<AgentSidebarConversationLog
		{items}
		{busy}
		{done}
		{availability}
		{suggestions}
		{starters}
		{decisions}
		open={toolOpen}
		{canAnswerHeld}
		{nextHeld}
		{thinking}
		bind:log
		onscroll={onLogScroll}
		{onsend}
		{onapprove}
		{ondecline}
		ontoggle={toggleToolOpen}
	/>

	<AgentSidebarComposer
		bind:draft
		{contexts}
		{tierChoice}
		{composerOpen}
		{held}
		{busy}
		{availability}
		{outcome}
		{composerNote}
		{instantCheckNote}
		bind:composer
		{ontier}
		{onremovecontext}
		onsubmit={send}
		{onstop}
		onkeydown={onComposerKey}
	/>
</aside>

<style>
	/* Direction A, Cursor-faithful: neutral greys, accent only on the decision
	 * button and Send. Tokens are local to the sidebar; the app theme is untouched. */
	.agent-sidebar {
		--sb-bg: #181818;
		--sb-fg: #d4d4d4;
		--sb-muted: #8b8b8b;
		--sb-line: #2b2b2b;
		--sb-user-bg: #222;
		--sb-input-bg: #1f1f1f;
		--sb-code-bg: #2a2a2a;
		--sb-btn-bg: #262626;
		--sb-btn-hover: #2e2e2e;
		--sb-accent: #e5e5e5;
		--sb-on-accent: #141414;
		--sb-held-line: #3a3a3a;
		--sb-held-bg: #1e1e1e;
		--sb-held-glow: rgba(255, 255, 255, 0.06);
		--sb-danger: var(--danger);
		--sb-ease: cubic-bezier(0.2, 0.8, 0.2, 1);

		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		width: 22.5rem;
		height: 100%;
		min-height: 32rem;
		box-sizing: border-box;
		background: var(--sb-bg);
		color: var(--sb-fg);
		border-left: 1px solid var(--sb-line);
		font: 13px/1.45 ui-sans-serif, system-ui, 'Segoe UI', sans-serif;
		animation: slide-in 220ms var(--sb-ease) both;
	}
	:global(html[data-theme='light']) .agent-sidebar {
		--sb-bg: var(--bg);
		--sb-fg: var(--fg);
		--sb-muted: var(--muted);
		--sb-line: var(--border);
		--sb-user-bg: var(--surface-raised);
		--sb-input-bg: var(--surface);
		--sb-code-bg: var(--surface-raised);
		--sb-btn-bg: var(--surface-raised);
		--sb-btn-hover: var(--surface-raised-hover);
		--sb-accent: var(--fg);
		--sb-on-accent: var(--bg);
		--sb-held-line: var(--border);
		--sb-held-bg: var(--surface);
		--sb-held-glow: color-mix(in srgb, var(--fg) 8%, transparent);
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border-bottom: 1px solid var(--sb-line);
	}
	.grow {
		flex: 1;
	}
	.agent-sidebar :global(svg) {
		width: 0.85rem;
		height: 0.85rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
		flex: none;
	}
	.agent-sidebar :global(button) {
		font: inherit;
		font-size: 0.95em;
		color: var(--sb-fg);
		background: var(--sb-btn-bg);
		border: 1px solid var(--sb-line);
		border-radius: 6px;
		padding: 4px 10px;
		cursor: pointer;
		display: inline-flex;
		align-items: center;
		gap: 6px;
		transition: background 120ms, color 120ms;
	}
	.agent-sidebar :global(button:hover:not(:disabled)) {
		background: var(--sb-btn-hover);
	}
	.agent-sidebar :global(button:disabled) {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.agent-sidebar :global(button:focus-visible),
	.agent-sidebar :global(select:focus-visible) {
		outline: 2px solid var(--sb-accent);
		outline-offset: 1px;
	}
	.agent-sidebar :global(button.primary) {
		background: var(--sb-accent);
		border-color: var(--sb-accent);
		color: var(--sb-on-accent);
		font-weight: 600;
	}
	.agent-sidebar :global(button.primary:hover:not(:disabled)) {
		background: color-mix(in srgb, var(--sb-accent) 88%, var(--sb-bg));
	}
	.agent-sidebar :global(button.icon) {
		background: transparent;
		border-color: transparent;
		color: var(--sb-muted);
		padding: 2px 4px;
	}
	.agent-sidebar :global(button.icon:hover:not(:disabled)) {
		background: transparent;
		color: var(--sb-fg);
	}

	@keyframes slide-in {
		from {
			transform: translateX(100%);
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.agent-sidebar,
		.agent-sidebar :global(*),
		.agent-sidebar :global(*)::before,
		.agent-sidebar :global(*)::after {
			animation: none !important;
			transition: none !important;
		}
	}
</style>
