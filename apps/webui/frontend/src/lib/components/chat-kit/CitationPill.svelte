<script lang="ts">
	import { citationStateTitle, type CitationRef } from '$lib/chat-kit/types';

	interface Props {
		citation: CitationRef;
	}

	const { citation }: Props = $props();
	const title = $derived(citation.title ?? citationStateTitle(citation.state));
</script>

{#if citation.href}
	<a
		class="chat-citation-pill"
		class:verified={citation.state === 'verified'}
		class:warn={citation.state === 'warn'}
		class:fail={citation.state === 'fail'}
		data-state={citation.state}
		href={citation.href}
		rel="noopener noreferrer"
		{title}
	>
		<span class="dot" aria-hidden="true"></span>
		{citation.label}
	</a>
{:else}
	<span
		class="chat-citation-pill"
		class:verified={citation.state === 'verified'}
		class:warn={citation.state === 'warn'}
		class:fail={citation.state === 'fail'}
		data-state={citation.state}
		{title}
	>
		<span class="dot" aria-hidden="true"></span>
		{citation.label}
	</span>
{/if}

<style>
	.chat-citation-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		max-width: 100%;
		padding: 0.15rem 0.45rem;
		border-radius: 999px;
		border: 1px solid var(--chat-citation-border, var(--border));
		font-size: 0.72rem;
		line-height: 1.25;
		text-decoration: none;
		color: var(--fg);
		background: var(--surface-raised);
	}
	a.chat-citation-pill:hover {
		text-decoration: none;
		background: var(--surface-raised-hover);
	}
	.dot {
		width: 0.45rem;
		height: 0.45rem;
		border-radius: 50%;
		flex-shrink: 0;
		background: currentColor;
	}
	.verified {
		color: var(--chat-citation-verified-fg, var(--success));
		border-color: var(--chat-citation-verified-border, var(--success));
		background: var(--chat-citation-verified-bg);
	}
	.warn {
		color: var(--chat-citation-warn-fg, var(--warning));
		border-color: var(--chat-citation-warn-border, var(--warning));
		background: var(--chat-citation-warn-bg);
	}
	.fail {
		color: var(--chat-citation-fail-fg, var(--danger));
		border-color: var(--chat-citation-fail-border, var(--danger));
		background: var(--chat-citation-fail-bg);
	}
</style>
