<script lang="ts">
	// LIBWS-04: the Crate Desk preview bar. Shows the focused track with its
	// real overview strip and readouts. Playback is inert: Library mode runs
	// no audio engine (PERFMODE-14), and a preview player that does not wake
	// one is planned, not built.
	import PreviewStrip from '$lib/components/rb/browser/PreviewStrip.svelte';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { formatDuration } from '$lib/library/inspector-summary';

	const INERT = 'not implemented - see PARITY-TODO';

	/** The most recently added row of the selection is the one in focus. */
	const row = $derived(
		libraryWorkspace.selected_rows.length === 0
			? null
			: libraryWorkspace.selected_rows[libraryWorkspace.selected_rows.length - 1]
	);
	let artFailed = $state(false);
	$effect(() => {
		void row?.stable_id;
		artFailed = false;
	});
</script>

<section class="library-preview rb-panel" data-testid="library-preview-bar">
	<button class="play rb-inert" disabled title={INERT} aria-label={INERT}>
		<svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
			<path d="M1 1 L9 6 L1 11 Z" fill="currentColor" />
		</svg>
	</button>
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
			/>
		</div>
		<dl class="readouts">
			<div>
				<dt>BPM</dt>
				<dd title="Analyzed tempo in beats per minute">{row.bpm === null ? '' : row.bpm.toFixed(2)}</dd>
			</div>
			<div>
				<dt>KEY</dt>
				<dd title="Musical key in Camelot notation">{row.key ?? ''}</dd>
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
