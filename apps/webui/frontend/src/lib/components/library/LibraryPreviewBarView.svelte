<script lang="ts">
	// Props-only half of LibraryPreviewBar (LIBWS-04): the container picks the
	// focused row from the Library selection and passes the overview strip in
	// as a snippet, since PreviewStrip is itself a container. The cover retry
	// state (artwork-cover.svelte) is global, so the container resolves it into
	// artworkSrc and the load/error callbacks; this half stays props-only.
	import type { Snippet } from 'svelte';
	import { formatDuration } from '$lib/library/inspector-summary';

	type PreviewPlayerStatus = 'idle' | 'loading' | 'playing' | 'paused' | 'error';

	/** The slice of BrowserRow this bar reads; the container passes the row itself. */
	type PreviewRow = {
		stable_id: string;
		title: string | null;
		artist: string | null;
		bpm: number | null;
		key: string | null;
		duration_ms: number | null;
		energy: number | null;
		artwork_available: boolean | null;
	};

	let {
		row,
		playing,
		elsewhere,
		stopHere,
		playTitle,
		refusal,
		previewStatus,
		otherPreviewStatus,
		keyLabel,
		artworkSrc,
		onArtworkLoad,
		onArtworkError,
		onPlay,
		onStop,
		previewStrip
	}: {
		row: PreviewRow | null;
		playing: boolean;
		elsewhere: boolean;
		stopHere: boolean;
		playTitle: string;
		refusal: string | null;
		previewStatus: PreviewPlayerStatus;
		otherPreviewStatus: PreviewPlayerStatus;
		keyLabel: string;
		/** The cover URL to draw, or null while there is no cover or a retry is pending. */
		artworkSrc: string | null;
		onArtworkLoad: () => void;
		onArtworkError: () => void;
		onPlay: () => void;
		onStop: () => void;
		/** The focused row's overview strip; rendered only while a row is focused. */
		previewStrip: Snippet;
	} = $props();

</script>

<section class="library-preview rb-panel" data-testid="library-preview-bar">
	<button
		class="play"
		class:playing
		data-testid="library-preview-play"
		data-preview-status={previewStatus}
		disabled={row === null || refusal !== null}
		aria-pressed={playing}
		title={playTitle}
		aria-label={playTitle}
		onclick={onPlay}
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
			data-preview-status={otherPreviewStatus}
			title="Stop the preview of the track you selected before"
			aria-label="Stop the preview of the track you selected before"
			onclick={onStop}
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
			data-preview-status={previewStatus}
			title="Stop the preview"
			aria-label="Stop the preview"
			onclick={onStop}
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
			{#if artworkSrc !== null}
				<img
					src={artworkSrc}
					alt=""
					width="36"
					height="36"
					data-expected-resource-error="artwork-404"
					onload={onArtworkLoad}
					onerror={onArtworkError}
				/>
			{/if}
		</div>
		<div class="ident">
			<span class="title" data-testid="library-preview-title">{row.title ?? '(untitled)'}</span>
			<span class="artist">{row.artist ?? ''}</span>
		</div>
		<div class="strip">
			{@render previewStrip()}
		</div>
		<dl class="readouts">
			<div>
				<dt>BPM</dt>
				<dd title="Analyzed tempo in beats per minute">{row.bpm === null ? '' : row.bpm.toFixed(2)}</dd>
			</div>
			<div>
				<dt>KEY</dt>
				<dd title="Musical key, in the key notation chosen in Settings">{keyLabel}</dd>
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
