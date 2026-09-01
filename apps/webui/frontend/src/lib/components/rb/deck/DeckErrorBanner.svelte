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
</script>

<div
	class="deck-error"
	role="alert"
	data-performance-error={deckId}
	data-performance-error-id={errorId}
	title={errorId === null ? error : `${error}\n\nError id ${errorId} - search the console or __mdtPerfLog() for it to find this exact failure.`}
>
	<span class="deck-error-text">{error}</span>
	{#if errorId !== null}
		<span
			class="deck-error-id"
			title={`Error id ${errorId}. This exact string is in the console line and the perf-event ring row for this failure.`}>{errorId}</span
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
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.deck-error-text {
		flex: 1 1 auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
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
