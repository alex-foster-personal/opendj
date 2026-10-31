<script lang="ts">
	import { shownKey } from '$lib/rb/key-notation';
	// LIBWS-04: the Crate Desk preview bar. Shows the focused track with its
	// real overview strip and readouts, and plays it through the Library's own
	// preview player (library-preview.svelte.ts): never the Gig engine, which
	// Library mode keeps torn down (PERFMODE-14). A strip click plays from there.
	import PreviewStrip from '$lib/components/rb/browser/PreviewStrip.svelte';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { formatDuration } from '$lib/library/inspector-summary';
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
	let artFailed = $state(false);
	$effect(() => {
		void row?.stable_id;
		artFailed = false;
	});
</script>

<section class="library-preview rb-panel" data-testid="library-preview-bar">
	<button
		class="play"
		class:playing
		data-testid="library-preview-play"
		data-preview-status={live ? libraryPreview.status : 'idle'}
		disabled={row === null || refusal !== null}
		aria-pressed={playing}
		title={playTitle}
		aria-label={playTitle}
		onclick={() => {
			if (row !== null) toggleLibraryPreview(row);
		}}
	>
		{#if playing}
			<svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
				<rect x="1" y="1" width="3" height="10" fill="currentColor" />
				<rect x="6" y="1" width="3" height="10" fill="currentColor" />
			</svg>
		{:else}
			<svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
				<path d="M1 1 L9 6 L1 11 Z" fill="currentColor" />
			</svg>
		{/if}
	</button>
	{#if elsewhere}
		<button
			class="stop-other"
			data-testid="library-preview-stop-other"
			data-preview-status={libraryPreview.status}
			title="Stop the preview of the track you selected before"
			aria-label="Stop the preview of the track you selected before"
			onclick={stopLibraryPreview}
		>
			<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
				<rect x="1" y="1" width="8" height="8" fill="currentColor" />
			</svg>
		</button>
	{/if}
	{#if stopHere}
		<button
			class="stop-other"
			data-testid="library-preview-stop"
			data-preview-status={libraryPreview.status}
			title="Stop the preview"
			aria-label="Stop the preview"
			onclick={stopLibraryPreview}
		>
			<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
				<rect x="1" y="1" width="8" height="8" fill="currentColor" />
			</svg>
		</button>
	{/if}
	{#if row === null}
		<p class="empty" data-testid="library-preview-empty">Select a track to preview its details.</p>
	{:else}
		<div class="art">
			{#if row.artwork_available === true && !artFailed}
				<img
					src={artworkUrl(row.stable_id, 's')}
					alt=""
					width="36"
					height="36"
					onerror={() => (artFailed = true)}
				/>
			{/if}
		</div>
		<div class="ident">
			<span class="title" data-testid="library-preview-title">{row.title ?? '(untitled)'}</span>
			<span class="artist">{row.artist ?? ''}</span>
		</div>
		<div class="strip">
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
		</div>
		<dl class="readouts">
			<div>
				<dt>BPM</dt>
				<dd title="Analyzed tempo in beats per minute">{row.bpm === null ? '' : row.bpm.toFixed(2)}</dd>
			</div>
			<div>
				<dt>KEY</dt>
				<dd title="Musical key, in the key notation chosen in Settings">{shownKey(row.key) ?? ''}</dd>
			</div>
			<div>
				<dt>TIME</dt>
				<dd title="Track length in minutes and seconds">
					{row.duration_ms === null ? '' : formatDuration(row.duration_ms)}
				</dd>
			</div>
			<div>
				<dt>ENERGY</dt>
				<dd title="Mixed In Key energy level, 1 to 9">{row.energy ?? ''}</dd>
			</div>
		</dl>
	{/if}
</section>

<style>
	.library-preview {
		grid-area: preview;
		display: flex;
		align-items: center;
		gap: 10px;
		height: 48px;
		padding: 0 10px;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		color: var(--rb-text);
		min-width: 0;
	}
	/* The bar sits between two lines it does not own: the top bar's bottom
	 * border above, and the desk's top edges below (the browser's border-top
	 * and the inspector's panel border). Drawing its own on either side
	 * doubled them (UIBORDER-03), so the panel chrome keeps only the sides. */
	.library-preview.rb-panel {
		border-top: 0;
		border-bottom: 0;
	}
	.play {
		width: 28px;
		height: 28px;
		display: grid;
		place-items: center;
		border-radius: 50%;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.play:hover:not(:disabled),
	.play.playing {
		color: var(--rb-text);
		border-color: var(--rb-accent, var(--rb-text-dim));
	}
	.stop-other {
		flex: none;
		width: 22px;
		height: 22px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 50%;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.play:disabled {
		cursor: default;
		opacity: 0.5;
	}
	.empty {
		margin: 0;
		color: var(--rb-text-dim);
	}
	.art {
		width: 36px;
		height: 36px;
		flex: none;
		background: var(--rb-bg);
		border: 1px solid var(--rb-border);
	}
	.art img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.ident {
		display: grid;
		min-width: 120px;
		max-width: 260px;
	}
	.ident span {
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.title {
		font-weight: 600;
	}
	.artist {
		color: var(--rb-text-dim);
	}
	.strip {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
	}
	.readouts {
		display: flex;
		gap: 14px;
		margin: 0;
	}
	.readouts div {
		display: grid;
		justify-items: end;
	}
	.readouts dt {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		letter-spacing: 0.05em;
	}
	.readouts dd {
		margin: 0;
		min-width: 36px;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
</style>
