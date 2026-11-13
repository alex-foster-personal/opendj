<script lang="ts">
	import {
		KEYLESS_SEND_TITLE,
		QUEUED_SEND_NOTE,
		type Availability
	} from '$lib/assistant/agent-labels';
	import { TIER_CHOICES } from './agent-sidebar-view-constants';
	import type { ContextChip, TierChoice } from './AgentSidebarView.svelte';

	interface Props {
		draft?: string;
		contexts?: ContextChip[];
		tierChoice?: TierChoice;
		composerOpen: boolean;
		held: boolean;
		busy: boolean;
		availability: Availability;
		outcome: 'send' | 'queue' | 'refuse' | 'ignore';
		composerNote: string | null;
		instantCheckNote: string | null;
		composer?: HTMLTextAreaElement | undefined;
		ontier: (choice: TierChoice) => void;
		onremovecontext?: ((id: string) => void) | undefined;
		onsubmit: () => void;
		onstop: () => void;
		onkeydown: (event: KeyboardEvent) => void;
	}

	let {
		draft = $bindable(''),
		contexts = [],
		tierChoice = 'auto',
		composerOpen,
		held,
		busy,
		availability,
		outcome,
		composerNote,
		instantCheckNote,
		composer = $bindable(),
		ontier,
		onremovecontext,
		onsubmit,
		onstop,
		onkeydown
	}: Props = $props();
</script>

<form
	class="composer"
	class:disabled={!composerOpen || held}
	onsubmit={(event) => {
		event.preventDefault();
		onsubmit();
	}}
>
	{#if contexts.length > 0}
		<div class="contexts">
			{#each contexts as context (context.id)}
				<span class="context" title={context.title}>
					@{context.label}
					{#if onremovecontext}
						<button
							type="button"
							aria-label="Remove {context.label}"
							onclick={() => onremovecontext(context.id)}
						>
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
		{onkeydown}
		rows="2"
		disabled={!composerOpen || held}
		placeholder={held
			? 'Answer the action above first'
			: availability === 'keyless'
				? 'Instant command, e.g. "play deck 1"'
				: 'Ask, or tell it what to do...'}
		aria-label="Message the assistant"></textarea>
	{#if composerNote}<p class="composer-note" role="status">{composerNote}</p>{/if}
	<div class="composer-row">
		<select
			class="tier-picker"
			aria-label="Model tier"
			value={tierChoice}
			title={TIER_CHOICES.find((choice) => choice.value === tierChoice)?.title}
			onchange={(event) => ontier(event.currentTarget.value as TierChoice)}
		>
			{#each TIER_CHOICES as choice (choice.value)}
				<option value={choice.value} title={choice.title}>{choice.label}</option>
			{/each}
		</select>
		<span class="grow"></span>
		{#if busy}
			<button type="button" class="send" title="Stop" onclick={onstop}>
				<svg viewBox="0 0 16 16" aria-hidden="true"
					><rect x="4" y="4" width="8" height="8" rx="1" /></svg
				>
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

<style>
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
	.grow {
		flex: 1;
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

	textarea:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}

	@media (prefers-reduced-motion: reduce) {
		.composer {
			transition: none !important;
		}
	}
</style>
