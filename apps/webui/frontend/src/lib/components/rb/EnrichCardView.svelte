<!--
	ENRICH-01: the enrich-on-open card, presentational half. Props in, no
	fetch, so every state has a Storybook story (EnrichCardView.stories.ts)
	and an approved image ((private path)/). The fetching
	half is EnrichCard.svelte; every word comes from `$lib/enrich/enrich-card`.

	Two parts (issue #5748, Wed 7 Oct 2026): a one-row pill that sits IN FLOW
	where it is mounted (the top bar), so it can never cover a control, and the
	expanded card, which floats below the pill only after a click and closes on
	Esc, on a click outside it, and on Less.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';
	import {
		type EnrichSummary,
		absentLines,
		dudLines,
		analysisLines,
		attentionTitle,
		collapsedLine,
		lyricsLine,
		offersRetry,
		songsTitle,
		stemsDeclinedText,
		stemsText,
		stemsTitle,
		stemsTone,
		warningLines,
		ingestGenreLine
	} from '$lib/enrich/enrich-card';
	import { suggestionsLine } from '$lib/enrich/artwork-suggestions';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';

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
		/** False: only the pill. True: the card floats below the pill. */
		expanded?: boolean;
		/** Something needs the user (needsAttention): the pill is flagged, never auto-expanded. */
		attention?: boolean;
		ontoggle?: () => void;
		/** Esc or a click outside the pill and the card. */
		onclose?: () => void;
		onhide?: () => void;
		onretry?: () => void;
		onask?: () => void;
		onnever?: () => void;
		/** ENRICH-05: take back a stored "never" (PUT answer "ask"). */
		onaskagain?: () => void;
		/** LIBM-189: open the store-cover review dialog. */
		onreviewcovers?: () => void;
	}

	const {
		summary,
		loadError = null,
		actionError = null,
		busy = false,
		asking = false,
		stemsPrompt,
		expanded = true,
		attention = false,
		ontoggle,
		onclose,
		onhide,
		onretry,
		onask,
		onnever,
		onaskagain,
		onreviewcovers
	}: Props = $props();

	const coversLine = $derived(summary ? suggestionsLine(summary.artwork_suggestions ?? 0) : null);

	const lines = $derived(
		summary ? [...analysisLines(summary), ...[lyricsLine(summary), ingestGenreLine(summary)].filter((l) => l !== null), ...dudLines(summary), ...absentLines(summary), ...warningLines(summary)] : []
	);
	const stemsLine = $derived(summary ? stemsText(summary.stems) : null);
	const declinedLine = $derived(summary ? stemsDeclinedText(summary) : null);
	const pillLine = $derived(
		summary ? collapsedLine(summary) : { text: 'Library status unknown', title: loadError ?? 'The library summary has not loaded' }
	);

	let pillEl = $state<HTMLElement | null>(null);
	let cardEl = $state<HTMLElement | null>(null);

	function onWindowKeydown(event: KeyboardEvent): void {
		if (expanded && event.key === 'Escape') onclose?.();
	}

	function onWindowPointerdown(event: PointerEvent): void {
		if (!expanded) return;
		const target = event.target as Node | null;
		if (target && (pillEl?.contains(target) || cardEl?.contains(target))) return;
		onclose?.();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerdown} />

<span class="enrich-pill-slot" bind:this={pillEl}>
	<button
		type="button"
		class="enrich-pill"
		class:attention
		onclick={ontoggle}
		aria-expanded={expanded}
		aria-label={`Library enrichment: ${pillLine.text}${attention ? ', needs you' : ''}`}
		title={`${pillLine.title}${attention ? `\n${summary ? attentionTitle(summary) : 'Something needs you: click to see it.'}` : ''}`}
		data-testid="enrich-pill"
	>
		<span class="enrich-dot" aria-hidden="true"></span>
		{#if attention}<span class="enrich-flag" data-testid="enrich-attention">needs you</span>{/if}
		<span class="enrich-pill-text" data-testid="enrich-collapsed">{pillLine.text}</span>
	</button>
</span>

{#if expanded}
	<section
		class="enrich-card"
		aria-label="Library enrichment"
		data-testid="enrich-card"
		bind:this={cardEl}
		use:triggerFloatingAction={{ getTrigger: () => pillEl, preferred: 'below', gap: 4 }}
	>
		<header>
			<h3 title={summary ? (songsTitle(summary) ?? undefined) : undefined}>Getting your library ready</h3>
			<span class="enrich-header-actions">
				<button type="button" class="enrich-hide" onclick={ontoggle} aria-expanded={expanded} title="Collapse to the top-bar pill"
					>Less</button
				>
				<button type="button" class="enrich-hide" onclick={onhide} title="Hide until the app is next opened">Hide</button>
			</span>
		</header>
		{#if loadError}
			<p class="enrich-line failed">{loadError}</p>
		{/if}
		{#each lines as line (line.lane + line.tone)}
			<p class="enrich-line {line.tone}" title={line.title ?? undefined} data-lane={line.lane} data-tone={line.tone}>
				{line.text}
			</p>
		{/each}
		{#if coversLine}
			<p class="enrich-line note" data-lane="artwork-suggestions" data-tone="note">{coversLine}</p>
			<div class="enrich-actions">
				<button type="button" class="enrich-go" onclick={onreviewcovers} disabled={onreviewcovers === undefined}
					data-testid="enrich-review-covers">Review covers...</button
				>
			</div>
		{/if}
		{#if summary && offersRetry(summary)}
			<button type="button" onclick={onretry} disabled={busy}>Retry failed analysis</button>
		{/if}
		{#if stemsLine}
			<p
				class="enrich-line {summary ? stemsTone(summary.stems) : 'unavailable'}"
				class:attention={summary?.stems.state === 'ask'}
				title={summary ? (stemsTitle(summary.stems) ?? undefined) : undefined}
				data-lane="stems"
				data-tone={summary ? stemsTone(summary.stems) : undefined}
			>
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
		{#if declinedLine}
			<p class="enrich-line unavailable" data-lane="stems" data-tone="declined">{declinedLine}</p>
			<div class="enrich-actions">
				<button
					type="button"
					onclick={onaskagain}
					disabled={busy || onaskagain === undefined}
					title="Undo Never: the card asks about separating this library's stems again"
					data-testid="enrich-stems-ask-again">Ask about stems again</button
				>
			</div>
		{/if}
		{#if actionError}
			<p class="enrich-line failed">{actionError}</p>
		{/if}
	</section>
{/if}

<style>
	/* The pill: in flow inside its mount (the top bar), so it takes its own
	   space and cannot sit over anything. Basis 0: it takes only the row's
	   FREE space (most of it, beside the spacers), never width another
	   control needs, and truncates inside it with the full text on hover.
	   Its floor is the dot, which carries the attention flag on its own. */
	.enrich-pill-slot {
		display: inline-flex;
		flex: 8 1 0;
		min-width: 22px;
		max-width: 360px;
		overflow: hidden;
	}
	.enrich-pill {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
		max-width: 100%;
		padding: 1px 7px;
		border-radius: 9px;
		color: var(--rb-text-dim);
		white-space: nowrap;
		align-self: center;
	}
	.enrich-pill[aria-expanded='true'] {
		border-color: var(--rb-accent);
	}
	.enrich-dot {
		flex: none;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--rb-text-dim);
	}
	.enrich-pill.attention .enrich-dot {
		background: var(--rb-accent);
		box-shadow: 0 0 0 2px var(--rb-accent-glow);
	}
	/* "needs you" takes --rb-warn-text, amber in every skin (Gothic declares it
	   explicitly), unlike --rb-orange, which Gothic grays (#10370). */
	.enrich-flag {
		flex: none;
		color: var(--rb-warn-text, #e8a13a);
		font-weight: 600;
	}
	.enrich-pill-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	/* The expanded card: only after a click, placed below the pill by
	   triggerFloatingAction (position: fixed, clamped to the viewport). */
	.enrich-card {
		position: fixed;
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
		text-align: left;
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
	.enrich-header-actions {
		display: flex;
		gap: 6px;
		margin-left: 12px;
	}
	.enrich-line {
		margin: 0;
		line-height: 16px;
	}
	.enrich-line.ready {
		/* Green; Gothic redefines --rb-green as white (theme.css). */
		color: var(--rb-green);
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
	.enrich-line.attention {
		/* Literal orange. Gothic sets --rb-orange to gray; this must not follow the skin. */
		color: #e8a13a;
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
