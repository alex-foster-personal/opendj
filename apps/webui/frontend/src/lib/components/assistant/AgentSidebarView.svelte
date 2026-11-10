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
	import { MAX_TOOL_ROUNDS, type DoneReason } from '$lib/assistant/agent-contract';
	import type { Item, ToolStatus } from '$lib/assistant/agent-session';
	import type { AssistantStatus } from '$lib/assistant/assistant-api';
	import {
		AVAILABILITY_NOTICE,
		KEYLESS_SEND_TITLE,
		TIER_LABEL,
		TIER_TITLE,
		QUEUED_SEND_NOTE,
		composerEscapeAction,
		errorText,
		heldKeyAction,
		instantCheckFailedNote,
		sendOutcome,
		toolLabel,
		type Availability
	} from '$lib/assistant/agent-labels';
	import { renderMarkdown } from '$lib/assistant/markdown';
	import { ASSISTANT_ROOT_ATTR } from '$lib/assistant/sidebar-toggle';
	import { untrack } from 'svelte';

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
		draft?: string;
		suggestions?: string[];
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
		draft = $bindable(''),
		suggestions = [],
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

	const CHOICES: { value: TierChoice; label: string; title: string }[] = [
		{ value: 'auto', label: 'Auto', title: 'Pick Instant, Fast or Think for each message' },
		{ value: 'fast', label: 'Fast', title: 'Always use the fast model' },
		{ value: 'think', label: 'Think', title: 'Always use the planning model' }
	];
	const STATUS_LABEL: Record<ToolStatus, string> = {
		running: 'Running',
		held: 'Needs your OK',
		ok: 'Done',
		failed: 'Failed',
		declined: 'Declined'
	};

	let open = $state<Record<string, boolean>>({});
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

	//----- formatting

	function shortModel(model: string | null): string {
		return model ? (model.split('/').at(-1) ?? model) : '';
	}

	function tierTitle(item: Extract<Item, { kind: 'tier' }>): string {
		return `${TIER_TITLE[item.tier]}. ${item.reason}. Model: ${item.model ?? 'none'}`;
	}

	function argSummary(args: Record<string, unknown>): string {
		return Object.values(args)
			.map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
			.join(' ');
	}

	function pretty(value: unknown): string {
		const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
		return text.length > 2000 ? `${text.slice(0, 2000)}\n...` : text;
	}

	function pendingLabel(decision: Decision | undefined): string | null {
		if (decision === 'approve') return 'Approved - waiting';
		if (decision === 'decline') return 'Declined - waiting';
		return null;
	}
</script>

<svelte:window onkeydown={onWindowKey} />

<aside class="agent-sidebar" aria-label="DJ assistant" bind:this={root} {...{ [ASSISTANT_ROOT_ATTR]: '' }}>
	<header>
		<h2>Assistant</h2>
		{#if mode === 'gateway'}
			<span class="plan" title="The assistant is free during the preview; a spend cap per install applies.">
				Free preview
			</span>
		{/if}
		<span class="grow"></span>
		<button type="button" class="icon" title="New chat" aria-label="New chat" onclick={onnewchat}>
			<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
		</button>
		<button type="button" class="icon" title="Close" aria-label="Close assistant" onclick={onclose}>
			<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
		</button>
	</header>

	<div class="log" role="log" aria-live="polite" aria-label="Conversation" bind:this={log} onscroll={onLogScroll}>
		{#if availability !== 'ready'}
			<p class="notice notice-{availability}" role="status">{AVAILABILITY_NOTICE[availability]}</p>
		{:else if items.length === 0}
			<p class="muted intro">
				Ask about your library, or tell it what to do. Anything that changes a deck, your
				library or this computer waits for your OK first.
			</p>
			<div class="suggestions">
				{#each suggestions as suggestion (suggestion)}
					<button type="button" class="suggestion" onclick={() => onsend(suggestion)}>{suggestion}</button>
				{/each}
			</div>
		{/if}

		{#each items as item, index (index)}
			{#if item.kind === 'user'}
				<p class="user">{item.text}</p>
			{:else if item.kind === 'tier'}
				<span class="tier" title={tierTitle(item)}>
					<b>{TIER_LABEL[item.tier]}</b>{#if item.model}<span class="model">{shortModel(item.model)}</span>{/if}
				</span>
			{:else if item.kind === 'text'}
				<div class="md" class:streaming={busy && index === items.length - 1}>
					{@html renderMarkdown(item.text)}
				</div>
			{:else if item.kind === 'tool'}
				{@const id = item.call.id}
				{@const label = toolLabel(item.call.name)}
				{@const summary = argSummary(item.call.arguments)}
				{@const pending = item.status === 'held' ? pendingLabel(decisions[id]) : null}
				<section class="tool status-{item.status}" aria-label={label} data-call-id={id}>
					<button
						type="button"
						class="tool-row"
						aria-expanded={open[id] === true}
						title="Tool {item.call.name} - click for arguments and result"
						onclick={() => (open = { ...open, [id]: !open[id] })}
					>
						<svg class="chevron" class:open={open[id]} viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" /></svg>
						<span class="tool-name">{label}</span>
						<span class="tool-arg" title={summary}>{summary}</span>
						{#if item.status === 'ok'}
							<svg class="tick" viewBox="0 0 16 16" role="img" aria-label="Done"><path d="M3.5 8.5l3 3 6-7" /></svg>
						{:else}
							<span class="tool-state">{pending ?? STATUS_LABEL[item.status]}</span>
						{/if}
					</button>
					{#if open[id]}
						<div class="tool-body">
							<p class="body-label">Arguments</p>
							<pre>{pretty(item.call.arguments)}</pre>
							{#if item.status === 'ok' || item.status === 'failed' || item.status === 'declined'}
								<p class="body-label">Result</p>
								<pre>{pretty(item.error ?? item.result)}</pre>
							{/if}
						</div>
					{/if}
					{#if item.status === 'failed' && item.error}
						<p class="tool-error">{item.error.message}</p>
					{/if}
					{#if item.status === 'held' && !pending}
						{#if item.reason}<p class="tool-reason">{item.reason}</p>{/if}
						<div class="tool-actions">
							<button type="button" class="primary" disabled={!canAnswerHeld} onclick={() => onapprove(id)}>
								Do it{#if nextHeld === item}<kbd>Enter</kbd>{/if}
							</button>
							<button type="button" disabled={!canAnswerHeld} onclick={() => ondecline(id)}>
								Don't{#if nextHeld === item}<kbd>Esc</kbd>{/if}
							</button>
						</div>
					{/if}
				</section>
			{:else if item.kind === 'error'}
				<div class="notice danger" role="alert">
					<p>{errorText(item.code, item.message)}</p>
					<details class="agent-details-only">
						<summary>Details for agents</summary>
						<pre>{item.code}</pre>
					</details>
				</div>
			{/if}
		{/each}

		{#if thinking}
			<p class="thinking">Working</p>
		{/if}
		{#if done === 'turn_cap'}
			<p class="notice">
				Stopped after
				<span class="num" title="Most model rounds one request may take before it stops and asks you">{MAX_TOOL_ROUNDS}</span>
				rounds. Say "keep going" to continue.
			</p>
		{/if}
	</div>

	<form
		class="composer"
		class:disabled={!composerOpen || held}
		onsubmit={(event) => {
			event.preventDefault();
			send();
		}}
	>
		{#if contexts.length > 0}
			<div class="contexts">
				{#each contexts as context (context.id)}
					<span class="context" title={context.title}>
						@{context.label}
						{#if onremovecontext}
							<button type="button" aria-label="Remove {context.label}" onclick={() => onremovecontext(context.id)}>
								<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
							</button>
						{/if}
					</span>
				{/each}
			</div>
		{/if}
		<textarea
			bind:this={composer}
			bind:value={draft}
			onkeydown={onComposerKey}
			rows="2"
			disabled={!composerOpen || held}
			placeholder={held
				? 'Answer the action above first'
				: availability === 'keyless'
					? 'Instant command, e.g. "play deck 1"'
					: 'Ask, or tell it what to do...'}
			aria-label="Message the assistant"
		></textarea>
		{#if composerNote}<p class="composer-note" role="status">{composerNote}</p>{/if}
		<div class="composer-row">
			<select
				class="tier-picker"
				aria-label="Model tier"
				value={tierChoice}
				title={CHOICES.find((choice) => choice.value === tierChoice)?.title}
				onchange={(event) => ontier(event.currentTarget.value as TierChoice)}
			>
				{#each CHOICES as choice (choice.value)}
					<option value={choice.value} title={choice.title}>{choice.label}</option>
				{/each}
			</select>
			<span class="grow"></span>
			{#if busy}
				<button type="button" class="send" title="Stop" onclick={onstop}>
					<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
					Stop
				</button>
			{:else}
				<button
					type="submit"
					class="send primary"
					title={outcome === 'refuse'
						? (instantCheckNote ?? KEYLESS_SEND_TITLE)
						: outcome === 'queue'
							? QUEUED_SEND_NOTE
							: 'Send (Enter)'}
					disabled={outcome !== 'send' && outcome !== 'queue'}
				>
					<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M4 7l4-4 4 4" /></svg>
					Send
				</button>
			{/if}
		</div>
	</form>
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
	h2 {
		margin: 0;
		font-size: 1em;
		font-weight: 600;
	}
	.plan {
		color: var(--sb-muted);
		font-size: 0.82em;
		border: 1px solid var(--sb-line);
		border-radius: 99px;
		padding: 0 7px;
		cursor: help;
	}
	.grow {
		flex: 1;
	}
	svg {
		width: 0.85rem;
		height: 0.85rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
		flex: none;
	}
	button {
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
	button:hover:not(:disabled) {
		background: var(--sb-btn-hover);
	}
	button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	button:focus-visible,
	select:focus-visible {
		outline: 2px solid var(--sb-accent);
		outline-offset: 1px;
	}
	button.primary {
		background: var(--sb-accent);
		border-color: var(--sb-accent);
		color: var(--sb-on-accent);
		font-weight: 600;
	}
	button.primary:hover:not(:disabled) {
		background: color-mix(in srgb, var(--sb-accent) 88%, var(--sb-bg));
	}
	button.icon {
		background: transparent;
		border-color: transparent;
		color: var(--sb-muted);
		padding: 2px 4px;
	}
	button.icon:hover:not(:disabled) {
		background: transparent;
		color: var(--sb-fg);
	}

	/* ----- log */
	.log {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 10px 12px;
	}
	.log > :global(*) {
		animation: enter 180ms ease-out both;
	}
	.muted {
		color: var(--sb-muted);
	}
	.intro {
		margin: 0;
	}
	.suggestions {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.suggestion {
		background: transparent;
		border-color: transparent;
		color: var(--sb-muted);
		justify-content: flex-start;
		text-align: left;
		padding: 4px 6px;
	}
	.suggestion:hover:not(:disabled) {
		background: var(--sb-user-bg);
		color: var(--sb-fg);
	}
	.user {
		margin: 6px 0 0;
		padding: 8px 11px;
		border: 1px solid var(--sb-line);
		border-radius: 6px;
		background: var(--sb-user-bg);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.tier {
		align-self: flex-start;
		display: inline-flex;
		gap: 5px;
		font-size: 0.8em;
		color: var(--sb-muted);
		border: 1px solid var(--sb-line);
		border-radius: 99px;
		padding: 0 8px;
		cursor: help;
	}
	.tier b {
		font-weight: 600;
		color: var(--sb-fg);
	}
	.model {
		font-family: ui-monospace, monospace;
		opacity: 0.8;
	}

	/* ----- markdown */
	.md {
		overflow-wrap: anywhere;
	}
	.md :global(:first-child) {
		margin-top: 0;
	}
	.md :global(:last-child) {
		margin-bottom: 0;
	}
	.md :global(p),
	.md :global(ul),
	.md :global(ol),
	.md :global(pre),
	.md :global(blockquote),
	.md :global(table) {
		margin: 0 0 6px;
	}
	.md :global(ul),
	.md :global(ol) {
		padding-left: 20px;
	}
	.md :global(li + li) {
		margin-top: 2px;
	}
	.md :global(h1),
	.md :global(h2),
	.md :global(h3),
	.md :global(h4) {
		font-size: 1.04em;
		margin: 10px 0 4px;
	}
	.md :global(code) {
		font: 0.92em ui-monospace, monospace;
		background: var(--sb-code-bg);
		border-radius: 4px;
		padding: 0 4px;
	}
	.md :global(pre) {
		background: var(--sb-input-bg);
		border: 1px solid var(--sb-line);
		border-radius: 6px;
		padding: 8px 10px;
		overflow-x: auto;
	}
	.md :global(pre code) {
		background: none;
		padding: 0;
	}
	.md :global(a) {
		color: var(--sb-fg);
		text-decoration: underline;
		text-decoration-color: var(--sb-muted);
	}
	.md :global(blockquote) {
		border-left: 2px solid var(--sb-line);
		padding-left: 10px;
		color: var(--sb-muted);
	}
	.md :global(table) {
		border-collapse: collapse;
		font-size: 0.92em;
	}
	.md :global(th),
	.md :global(td) {
		border: 1px solid var(--sb-line);
		padding: 2px 6px;
	}
	.md.streaming > :global(:last-child:not(ul, ol))::after,
	.md.streaming > :global(:is(ul, ol):last-child > li:last-child)::after,
	.thinking::after {
		content: '';
		display: inline-block;
		width: 0.5em;
		height: 1em;
		margin-left: 1px;
		vertical-align: -2px;
		background: var(--sb-fg);
		animation: blink 1s steps(1) infinite;
	}
	.thinking {
		margin: 0;
		color: var(--sb-muted);
	}

	/* ----- tool rows */
	.tool {
		display: flex;
		flex-direction: column;
		gap: 4px;
		border-radius: 6px;
	}
	.tool-row {
		background: transparent;
		border-color: transparent;
		color: var(--sb-muted);
		padding: 1px 3px;
		margin-left: -3px;
		width: calc(100% + 6px);
		text-align: left;
		white-space: nowrap;
	}
	.tool-row:hover:not(:disabled) {
		color: var(--sb-fg);
		background: var(--sb-user-bg);
	}
	.chevron {
		transition: transform 120ms;
	}
	.chevron.open {
		transform: rotate(90deg);
	}
	.tool-name {
		flex: none;
		color: var(--sb-fg);
	}
	.tool-arg {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.tool-state {
		flex: none;
		font-size: 0.85em;
	}
	.status-running .tool-name {
		background: linear-gradient(90deg, var(--sb-fg) 0 40%, var(--sb-muted) 50%, var(--sb-fg) 60% 100%) 0 / 250% 100%;
		background-clip: text;
		-webkit-background-clip: text;
		color: transparent;
		animation: shimmer 1.2s linear infinite;
	}
	.tick {
		color: var(--sb-muted);
		stroke-width: 2;
		animation: pop 250ms ease-out both;
	}
	.status-held {
		padding: 8px 10px;
		border: 1px solid var(--sb-held-line);
		background: var(--sb-held-bg);
		animation:
			enter 180ms ease-out both,
			pulse 1.6s ease-in-out infinite;
	}
	.status-held .tool-state,
	.tool-reason {
		color: var(--sb-accent);
	}
	.status-failed .tool-state,
	.tool-error {
		color: var(--sb-danger);
	}
	.status-declined .tool-name {
		text-decoration: line-through;
		color: var(--sb-muted);
	}
	.tool-body {
		margin-left: 18px;
		border-left: 1px solid var(--sb-line);
		padding-left: 10px;
	}
	.body-label {
		margin: 3px 0 0;
		font-size: 0.82em;
		color: var(--sb-muted);
	}
	pre {
		margin: 2px 0 0;
		max-height: 12rem;
		overflow: auto;
		font-size: 0.85em;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.tool-reason,
	.tool-error {
		margin: 0 0 4px 18px;
	}
	.tool-actions {
		display: flex;
		gap: 8px;
		margin-left: 18px;
	}
	kbd {
		font: 0.82em ui-monospace, monospace;
		opacity: 0.7;
		border: 1px solid currentColor;
		border-radius: 4px;
		padding: 0 4px;
	}

	/* ----- notices */
	.notice {
		margin: 0;
		border: 1px solid var(--sb-line);
		border-radius: 6px;
		padding: 8px 11px;
		color: var(--sb-muted);
	}
	.notice p {
		margin: 0;
	}
	.notice.danger {
		color: var(--sb-danger);
		border-color: color-mix(in srgb, var(--sb-danger) 50%, var(--sb-line));
	}
	.agent-details-only summary {
		cursor: pointer;
		color: var(--sb-muted);
		font-size: 0.85em;
	}
	.num {
		cursor: help;
		text-decoration: underline dotted;
	}

	/* ----- composer */
	.composer {
		margin: 0 12px 10px;
		border: 1px solid var(--sb-line);
		border-radius: 8px;
		background: var(--sb-input-bg);
		padding: 8px;
		display: flex;
		flex-direction: column;
		gap: 6px;
		transition: border-color 120ms;
	}
	.composer:focus-within {
		border-color: var(--sb-muted);
	}
	.composer.disabled {
		opacity: 0.6;
	}
	.contexts {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.context {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		font-size: 0.85em;
		color: var(--sb-muted);
		border: 1px solid var(--sb-line);
		border-radius: 5px;
		padding: 0 2px 0 6px;
		cursor: help;
	}
	.context button {
		background: transparent;
		border: none;
		padding: 1px;
		color: var(--sb-muted);
	}
	.context svg {
		width: 0.65rem;
		height: 0.65rem;
	}
	textarea {
		width: 100%;
		min-height: 34px;
		box-sizing: border-box;
		resize: none;
		background: transparent;
		color: var(--sb-fg);
		border: none;
		outline: none;
		padding: 0 2px;
		font: inherit;
	}
	textarea::placeholder {
		color: var(--sb-muted);
	}
	.composer-note {
		margin: 0;
		padding: 0 2px;
		font-size: 0.85em;
		color: var(--sb-muted);
	}
	.composer-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.tier-picker {
		font: inherit;
		font-size: 0.88em;
		color: var(--sb-muted);
		background: transparent;
		border: 1px solid transparent;
		border-radius: 4px;
		padding: 1px 3px;
	}
	.tier-picker:hover {
		border-color: var(--sb-line);
		color: var(--sb-fg);
	}
	.send {
		border: 0;
		padding: 3px 10px;
	}

	@keyframes slide-in {
		from {
			transform: translateX(100%);
			opacity: 0;
		}
	}
	@keyframes enter {
		from {
			opacity: 0;
			transform: translateY(6px);
		}
	}
	@keyframes shimmer {
		to {
			background-position: -250% 0;
		}
	}
	@keyframes pop {
		from {
			transform: scale(0.4);
			opacity: 0;
		}
	}
	@keyframes pulse {
		50% {
			box-shadow: 0 0 0 4px var(--sb-held-glow);
		}
	}
	@keyframes blink {
		50% {
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

	textarea:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
</style>
