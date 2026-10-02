<!--
	ENRICH-01: the enrich-on-open card, presentational half. Props in, no
	fetch, so every state has a Storybook story (EnrichCardView.stories.ts)
	and an approved image (specs/ui-contracts/enrich-on-open/). The fetching
	half is EnrichCard.svelte; every word comes from `$lib/enrich/enrich-card`.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';
	import {
		type EnrichSummary,
		analysisLines,
		lyricsLine,
		offersRetry,
		stemsText
	} from '$lib/enrich/enrich-card';

	interface Props {
		summary: EnrichSummary | null;
		/** The summary could not be fetched: shown as a failed line. */
		loadError?: string | null;
		/** A button's request failed: shown under the buttons. */
		actionError?: string | null;
		/** A request is in flight: Retry and Never are disabled. */
		busy?: boolean;
		/** The user chose "Separate stems...": render `stemsPrompt` in place of the three answers. */
		asking?: boolean;
		stemsPrompt?: Snippet;
		onhide?: () => void;
		onretry?: () => void;
		onask?: () => void;
		onnever?: () => void;
	}

	const {
		summary,
		loadError = null,
		actionError = null,
		busy = false,
		asking = false,
		stemsPrompt,
		onhide,
		onretry,
		onask,
		onnever
	}: Props = $props();

	const lines = $derived(summary ? [...analysisLines(summary), ...[lyricsLine(summary)].filter((l) => l !== null)] : []);
	const stemsLine = $derived(summary ? stemsText(summary.stems) : null);
</script>

<section class="enrich-card" aria-label="Library enrichment" data-testid="enrich-card">
	<header>
		<h3>Getting your library ready</h3>
		<button type="button" class="enrich-hide" onclick={onhide} title="Hide until the app is next opened">Hide</button>
	</header>
	{#if loadError}
		<p class="enrich-line failed">{loadError}</p>
	{/if}
	{#each lines as line (line.lane + line.tone)}
		<p class="enrich-line {line.tone}" title={line.title ?? undefined} data-lane={line.lane} data-tone={line.tone}>
			{line.text}
		</p>
	{/each}
	{#if summary && offersRetry(summary)}
		<button type="button" onclick={onretry} disabled={busy}>Retry failed analysis</button>
	{/if}
	{#if stemsLine}
		<p class="enrich-line {summary?.stems.state === 'ask' ? 'working' : 'unavailable'}" data-lane="stems">
			{stemsLine}
		</p>
	{/if}
	{#if summary?.stems.state === 'ask'}
		{#if asking && stemsPrompt}
			{@render stemsPrompt()}
		{:else}
			<div class="enrich-actions">
				<button type="button" class="enrich-go" onclick={onask}>Separate stems...</button>
				<button type="button" onclick={onhide}>Not now</button>
				<button type="button" onclick={onnever} disabled={busy}>Never for this library</button>
			</div>
		{/if}
	{/if}
	{#if actionError}
		<p class="enrich-line failed">{actionError}</p>
	{/if}
</section>

<style>
	.enrich-card {
		position: fixed;
		right: 12px;
		/* Clear of the feedback and help buttons that own the bottom-right corner. */
		bottom: 48px;
		z-index: 40;
		width: min(380px, calc(100vw - 24px));
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px 12px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		box-shadow: 0 4px 16px rgb(0 0 0 / 35%);
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	h3 {
		margin: 0;
		font-size: 13px;
		letter-spacing: 0.04em;
	}
	.enrich-line {
		margin: 0;
		line-height: 16px;
	}
	.enrich-line.working {
		color: var(--rb-text);
	}
	.enrich-line.note {
		color: var(--rb-text-dim);
	}
	.enrich-line.failed {
		color: var(--rb-red);
	}
	.enrich-line.unavailable {
		color: var(--rb-text-dim);
		font-style: italic;
	}
	.enrich-actions {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	button {
		font: inherit;
		font-size: 11px;
		padding: 4px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		cursor: pointer;
		align-self: flex-start;
	}
	.enrich-go {
		border-color: var(--rb-accent);
	}
	.enrich-hide {
		padding: 2px 8px;
	}
</style>
