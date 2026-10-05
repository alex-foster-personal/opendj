<!--
	Dismissable deck error banner.

	The banner used to be clearable only as a side effect of the next command
	that happened to touch the same deck, so an error raised by a step nothing
	would retry (AutoPlay's master handover) stuck to the deck for the rest of
	the set with no way to get rid of it. Dismissal does not mask the condition:
	it re-reports if it recurs.

	IT ALSO CARRIES AN ID. Because it never fades, this banner can stand on a
	deck for a whole set, and it used to render the error string and nothing
	else -- so the words on screen tied to no row in any log, and two identical
	failures an hour apart were indistinguishable. `errorId` is minted by
	`rb/deck-error-id.svelte.ts` BEFORE the log row is written, from the same
	counter `pushToast` mints from, and the string shown here is the string that
	row carries. Searching the console or `__mdtPerfLog()` for it finds the one
	raising it belongs to.
-->
<script lang="ts">
	import { classifyToastError } from '$lib/toast-error-classification';
	import { formatToastPresentation } from '$lib/toast-presentation';
	import { buildToastReport, writeToastReport } from '$lib/toast-report';

	// Presentational: the parent owns the deck identity, the id and the dismiss
	// action. `deckId` is a plain number here because it is only stamped into
	// the test hook and the label; Deck.svelte keeps the typed DeckId.
	//
	// `errorId` is nullable because the id is minted by an $effect that runs
	// after the derivation which reveals the banner, so there is one frame in
	// which the error is showing and the id has not landed yet. Rendering
	// nothing for that frame is correct; rendering a placeholder that looks
	// like an id would be a search that leads nowhere.
	let {
		deckId,
		error,
		errorId,
		onDismiss
	}: { deckId: number; error: string; errorId: string | null; onDismiss: () => void } = $props();

	const diagnostic = $derived(
		classifyToastError({
			kind: 'error',
			message: error,
			context: { deck_id: deckId, source: 'deck-error-banner' }
		})
	);

	const presentation = $derived(
		formatToastPresentation({
			kind: 'error',
			message: error,
			diagnostic
		})
	);

	// The load failure string often has no deck number of its own. The plain
	// headline starts with "couldn't load", and this banner knows the deck.
	const shownHeadline = $derived(
		presentation.headline.startsWith("couldn't load ")
			? `Deck ${deckId}: ${presentation.headline}`
			: presentation.headline
	);

	let copyNote = $state<string | null>(null);

	async function copyReport(): Promise<void> {
		if (errorId === null) return;
		const href = typeof window === 'undefined' ? '' : (window.location?.href ?? '');
		const page = href === '' ? 'unknown' : href.split('?')[0].split('#')[0];
		const text = buildToastReport({
			id: errorId,
			kind: 'error',
			headline: shownHeadline,
			message: error,
			detail: presentation.detail,
			classification: diagnostic.classification,
			settingsSummary: diagnostic.settingsSummary,
			hint: diagnostic.hint,
			createdAt: new Date().toISOString(),
			env: {
				machine: 'unknown',
				user: 'signed out',
				client: { name: 'unknown', version: 'unknown' },
				url: page
			}
		});
		try {
			await writeToastReport(
				text,
				typeof navigator === 'undefined' ? undefined : navigator.clipboard,
				typeof window !== 'undefined' && window.isSecureContext === true
			);
			copyNote = 'copied';
			setTimeout(() => {
				copyNote = null;
			}, 1500);
		} catch (exc) {
			copyNote = exc instanceof Error ? exc.message : String(exc);
		}
	}
</script>

<div
	class="deck-error"
	role="alert"
	data-performance-error={deckId}
	data-performance-error-id={errorId}
	title={errorId === null
		? shownHeadline
		: `${shownHeadline}\n\nError id ${errorId} - click to copy full report.`}
>
	<button type="button" class="deck-error-body" onclick={() => copyReport()}>
		<span class="deck-error-text">{shownHeadline}</span>
		<span class="deck-error-copy-hint">Click to copy</span>
		{#if copyNote !== null}
			<span class="deck-error-copy-note">{copyNote}</span>
		{/if}
	</button>
	{#if errorId !== null}
		<span
			class="deck-error-id"
			title={`Error id ${errorId}. This exact string is in the console line and the perf-event ring row for this failure.`}
			>{errorId}</span
		>
	{/if}
	<button
		type="button"
		class="deck-error-dismiss"
		data-performance-error-dismiss={deckId}
		title="Dismiss this deck error"
		aria-label={`Dismiss deck ${deckId} error`}
		onclick={onDismiss}>x</button
	>
</div>

<style>
	.deck-error {
		display: flex;
		align-items: center;
		position: absolute;
		z-index: 4;
		left: 8px;
		right: 8px;
		bottom: 3px;
		padding: 2px 5px;
		border: 1px solid var(--rb-red);
		background: rgba(30, 5, 5, 0.94);
		color: #ff8e87;
		font-size: var(--rb-fs-label);
	}

	.deck-error-body {
		flex: 1 1 auto;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		min-width: 0;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 0;
		cursor: pointer;
	}

	.deck-error-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}

	.deck-error-copy-hint {
		font-size: 0.85em;
		opacity: 0.75;
	}

	.deck-error-copy-note {
		font-size: 0.85em;
		opacity: 0.9;
	}

	/* Dimmer and monospace: the id is a search term a reader copies, not part
	 * of the sentence, and it must not compete with the failure text for the
	 * eye. It never shrinks below the message, which is why it does not flex. */
	.deck-error-id {
		flex: 0 0 auto;
		margin-left: 6px;
		font-family: var(--rb-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
		font-size: var(--rb-fs-label);
		opacity: 0.75;
		user-select: all;
		cursor: text;
	}

	.deck-error-dismiss {
		flex: 0 0 auto;
		margin-left: 6px;
		padding: 0 4px;
		border: 1px solid var(--rb-red);
		background: transparent;
		color: inherit;
		font: inherit;
		line-height: 1.2;
		cursor: pointer;
	}

	.deck-error-dismiss:hover {
		background: var(--rb-red);
		color: #fff;
	}
</style>
