<script lang="ts">
	import { shownKey } from '$lib/rb/key-notation';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { coverFailed, coverLoaded, coverSrc, coverVisible } from '$lib/rb/artwork-cover.svelte';
	// LIBWS-04: the Crate Desk preview bar. Shows the focused track with its
	// real overview strip and readouts, and plays it through the Library's own
	// preview player (library-preview.svelte.ts): never the Gig engine, which
	// Library mode keeps torn down (PERFMODE-14). A strip click plays from there.
	import PreviewStrip from '$lib/components/rb/browser/PreviewStrip.svelte';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import LibraryPreviewBarView from './LibraryPreviewBarView.svelte';
	import {
		libraryAudioRefusal,
		libraryPreview,
		previewLibraryRow,
		stopLibraryPreview,
		toggleLibraryPreview
	} from '$lib/library/library-preview.svelte';

	/** The most recently added row of the selection is the one in focus. */
	const row = $derived(
		libraryWorkspace.selected_rows.length === 0
			? null
			: libraryWorkspace.selected_rows[libraryWorkspace.selected_rows.length - 1]
	);
	/** This row is the one the preview player holds (playing, paused or loading). */
	const live = $derived(row !== null && libraryPreview.stable_id === row.stable_id);
	/**
	 * A preview of a track that is no longer in focus is still audible (or
	 * about to be): the selection moved, the player did not. The bar keeps a
	 * Stop for it, since the row in focus cannot reach that player.
	 */
	const elsewhere = $derived(
		!live &&
			libraryPreview.stable_id !== null &&
			(libraryPreview.status === 'playing' ||
				libraryPreview.status === 'loading' ||
				libraryPreview.status === 'paused')
	);
	/**
	 * The row in focus is the one held, but it has no overview strip, so the
	 * strip's own Stop is not drawn (Codex P2, PR #6189): the bar draws one, or a
	 * slow load could not be cancelled and a paused preview never fully stopped.
	 */
	const stopHere = $derived(
		live &&
			(row?.strip ?? null) === null &&
			(libraryPreview.status === 'playing' ||
				libraryPreview.status === 'loading' ||
				libraryPreview.status === 'paused')
	);
	const playing = $derived(live && libraryPreview.status === 'playing');
	const loading = $derived(live && libraryPreview.status === 'loading');
	const refusal = $derived(row === null ? null : libraryAudioRefusal(row));
	const playTitle = $derived(
		row === null
			? 'Select a track to preview it'
			: refusal !== null
				? `Cannot preview: ${refusal}`
				: loading
					? 'Loading the preview...'
					: playing
						? 'Pause the preview'
						: 'Play a preview of this track (Library mode plays it without starting the decks)'
	);
	const nowRatio = $derived(
		live && libraryPreview.duration_ms > 0
			? libraryPreview.position_ms / libraryPreview.duration_ms
			: null
	);
	const keyLabel = $derived(row === null ? '' : (shownKey(row.key) ?? ''));
	/** LIBM-185: the shared cover retry decides whether the cover shows and with which URL. */
	const artworkSrc = $derived(
		row !== null && row.artwork_available === true && coverVisible(row.stable_id)
			? coverSrc(artworkUrl(row.stable_id, 's'), row.stable_id)
			: null
	);
</script>

<LibraryPreviewBarView
	{row}
	{playing}
	{elsewhere}
	{stopHere}
	{playTitle}
	{refusal}
	previewStatus={live ? libraryPreview.status : 'idle'}
	otherPreviewStatus={libraryPreview.status}
	{keyLabel}
	{artworkSrc}
	onArtworkLoad={() => {
		if (row !== null) coverLoaded(row.stable_id);
	}}
	onArtworkError={() => {
		if (row !== null) coverFailed(row.stable_id);
	}}
	onPlay={() => {
		if (row !== null) toggleLibraryPreview(row);
	}}
	onStop={stopLibraryPreview}
>
	{#snippet previewStrip()}
		{#if row !== null}
			<PreviewStrip
				strip={row.strip ?? null}
				vocals={row.vocals ?? null}
				duration_ms={row.duration_ms}
				revealed={true}
				stable_id={row.stable_id}
				enabled={false}
				{nowRatio}
				previewing={live}
				onseek={(ratio) => {
					if (row !== null) previewLibraryRow(row, ratio);
				}}
				onstop={stopLibraryPreview}
			/>
		{/if}
	{/snippet}
</LibraryPreviewBarView>
