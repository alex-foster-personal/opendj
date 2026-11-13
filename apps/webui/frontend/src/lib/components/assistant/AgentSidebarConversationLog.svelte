<script lang="ts">
	import { MAX_TOOL_ROUNDS, type DoneReason } from '$lib/assistant/agent-contract';
	import type { Item } from '$lib/assistant/agent-session';
	import {
		AVAILABILITY_NOTICE,
		TIER_LABEL,
		errorText,
		toolLabel,
		type Availability
	} from '$lib/assistant/agent-labels';
	import { renderMarkdown } from '$lib/assistant/markdown';
	import type { Snippet } from 'svelte';
	import { TOOL_STATUS_LABEL } from './agent-sidebar-view-constants';
	import {
		argSummary,
		pendingLabel,
		pretty,
		shortModel,
		tierTitle
	} from './agent-sidebar-view-format';
	import type { Decision } from './AgentSidebarView.svelte';

	interface Props {
		items: Item[];
		busy: boolean;
		done: DoneReason | null;
		availability: Availability;
		suggestions?: string[];
		/** OJ starter chips (#14040), drawn under the intro while the log is empty. */
		starters?: Snippet | undefined;
		decisions?: Record<string, Decision>;
		open: Record<string, boolean>;
		canAnswerHeld: boolean;
		nextHeld: Item | undefined;
		thinking: boolean;
		log?: HTMLDivElement | undefined;
		onscroll?: () => void;
		onsend: (text: string) => void;
		onapprove: (callId: string) => void;
		ondecline: (callId: string) => void;
		ontoggle: (id: string) => void;
	}

	let {
		items,
		busy,
		done,
		availability,
		suggestions = [],
		starters,
		decisions = {},
		open,
		canAnswerHeld,
		nextHeld,
		thinking,
		log = $bindable(),
		onscroll,
		onsend,
		onapprove,
		ondecline,
		ontoggle
	}: Props = $props();
</script>

<div class="log" role="log" aria-live="polite" aria-label="Conversation" bind:this={log} {onscroll}>
	{#if availability !== 'ready'}
		<p class="notice notice-{availability}" role="status">{AVAILABILITY_NOTICE[availability]}</p>
	{:else if items.length === 0}
		<p class="muted intro">
			Ask about your library, or tell it what to do. Anything that changes a deck, your library or
			this computer waits for your OK first.
		</p>
		<div class="suggestions">
			{#each suggestions as suggestion (suggestion)}
				<button type="button" class="suggestion" onclick={() => onsend(suggestion)}
					>{suggestion}</button
				>
			{/each}
		</div>
		{@render starters?.()}
	{/if}

	{#each items as item, index (index)}
		{#if item.kind === 'user'}
			<p class="user">{item.text}</p>
		{:else if item.kind === 'tier'}
			<span class="tier" title={tierTitle(item)}>
				<b>{TIER_LABEL[item.tier]}</b>{#if item.model}<span class="model"
						>{shortModel(item.model)}</span
					>{/if}
			</span>
		{:else if item.kind === 'text'}
			<div class="md" class:streaming={busy && index === items.length - 1}>
				{@html renderMarkdown(item.text)}
			</div>
		{:else if item.kind === 'tool'}
			{@const id = item.call.id}
			{@const label = toolLabel(item.call.name, item.call.arguments)}
			{@const summary = argSummary(item.call.arguments)}
			{@const pending = item.status === 'held' ? pendingLabel(decisions[id]) : null}
			<section class="tool status-{item.status}" aria-label={label} data-call-id={id}>
				<button
					type="button"
					class="tool-row"
					aria-expanded={open[id] === true}
					title="Tool {item.call.name} - click for arguments and result"
					onclick={() => ontoggle(id)}
				>
					<svg class="chevron" class:open={open[id]} viewBox="0 0 16 16" aria-hidden="true"
						><path d="M6 4l4 4-4 4" /></svg
					>
					<span class="tool-name">{label}</span>
					<span class="tool-arg" title={summary}>{summary}</span>
					{#if item.status === 'ok'}
						<svg class="tick" viewBox="0 0 16 16" role="img" aria-label="Done"
							><path d="M3.5 8.5l3 3 6-7" /></svg
						>
					{:else}
						<span class="tool-state">{pending ?? TOOL_STATUS_LABEL[item.status]}</span>
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
						<button
							type="button"
							class="primary"
							disabled={!canAnswerHeld}
							onclick={() => onapprove(id)}
						>
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
			<span class="num" title="Most model rounds one request may take before it stops and asks you"
				>{MAX_TOOL_ROUNDS}</span
			>
			rounds. Say "keep going" to continue.
		</p>
	{/if}
</div>

<style>
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
		font:
			0.92em ui-monospace,
			monospace;
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
		background: linear-gradient(
				90deg,
				var(--sb-fg) 0 40%,
				var(--sb-muted) 50%,
				var(--sb-fg) 60% 100%
			)
			0 / 250% 100%;
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
		font:
			0.82em ui-monospace,
			monospace;
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
		.log,
		.log :global(*),
		.log :global(*)::before,
		.log :global(*)::after {
			animation: none !important;
			transition: none !important;
		}
	}
</style>
