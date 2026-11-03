<script lang="ts">
	// Props-only half of TrackInspector (LIBWS-02): the Crate Desk inspector's
	// panel. The container follows the selection, loads the track and its
	// playlists, and owns the one-at-a-time save queue; this file draws one
	// track's fields with their provenance chips, or the multi-select summary,
	// and hands every edit back through `applyPatch` and `addTag`. The Tempo
	// pref popover and the conflict dialog stay in the container.
	import type { components } from '$lib/api-types';
	import RatingStars from '$lib/components/rb/browser/RatingStars.svelte';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { CLOSE_PATH } from '$lib/ui/icon-glyphs';
	import type { TrackPlaylistHit } from '$lib/rb/track-playlists';
	import {
		formatBpmRange,
		formatDuration,
		readoutText,
		sourceLabel,
		type InspectorRow,
		type SelectionSummary
	} from '$lib/library/inspector-summary';

	type Track = components['schemas']['TrackOut'];
	type Patch = { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string };

	let {
		rowCount,
		paneTitle,
		summary,
		single,
		track,
		loadError,
		saveError,
		playlists,
		playlistsError,
		artFailed,
		newTag = $bindable(),
		applyPatch,
		addTag,
		onArtError,
		onTempoPref
	}: {
		rowCount: number;
		paneTitle: string;
		/** The multi-select summary; null unless several rows are selected. */
		summary: SelectionSummary | null;
		/** The one selected browser row; null unless exactly one is selected. */
		single: (InspectorRow & { artwork_available: boolean | null }) | null;
		/** The loaded track and its provenance; null until it arrives. */
		track: Track | null;
		loadError: string | null;
		saveError: string | null;
		playlists: TrackPlaylistHit[] | null;
		playlistsError: string | null;
		artFailed: boolean;
		newTag: string;
		applyPatch: (patch: Patch) => Promise<void>;
		addTag: () => void;
		onArtError: () => void;
		/** Open the Tempo pref editor anchored at the click. */
		onTempoPref: (x: number, y: number) => void;
	} = $props();

	/** Provenance chip text for a field, or null when nothing recorded who set it. */
	function sourceOf(field: string): string | null {
		const entry = track?.provenance[field];
		if (entry === undefined || entry.source.trim() === '') return null;
		return sourceLabel(entry.source);
	}

	function sourceTitle(field: string): string {
		const entry = track?.provenance[field];
		if (entry === undefined) return '';
		const confidence =
			entry.confidence === null || entry.confidence === undefined
				? ''
				: `, confidence ${Math.round(entry.confidence * 100)}%`;
		return `Set by ${entry.source} on ${entry.modified_at}${confidence}`;
	}
</script>

<aside class="inspector rb-panel" data-testid="track-inspector">
	{#if rowCount === 0}
		<p class="hint" data-testid="inspector-empty">Select tracks in the browser to inspect and edit them.</p>
	{:else if summary !== null}
		<header>
			<h2 data-testid="inspector-multi-heading">{summary.count} tracks selected</h2>
			<p class="sub">{paneTitle}</p>
		</header>
		<dl class="fields">
			<dt>Total time</dt>
			<dd
				title={summary.unknown_duration_count === 0
					? 'Sum of the selected tracks\' lengths'
					: `Sum of the known lengths; ${summary.unknown_duration_count} tracks have no length yet`}
			>
				{summary.total_duration_ms === null ? '' : formatDuration(summary.total_duration_ms)}
				{#if summary.unknown_duration_count > 0}
					<span class="dim">({summary.unknown_duration_count} unknown)</span>
				{/if}
			</dd>
			<dt>BPM</dt>
			<dd title="Lowest and highest analyzed tempo in the selection">
				{formatBpmRange(summary.bpm_min, summary.bpm_max)}
			</dd>
			<dt>Keys</dt>
			<dd title="Distinct keys in the selection, in Camelot order">{summary.keys.join(' ')}</dd>
			<dt>Artist</dt>
			<dd class:mixed={summary.artist.kind === 'mixed'}>{readoutText(summary.artist)}</dd>
			<dt>Genre</dt>
			<dd class:mixed={summary.genre.kind === 'mixed'}>{readoutText(summary.genre)}</dd>
			<dt>Rating</dt>
			<dd class:mixed={summary.rating.kind === 'mixed'} title="Star rating, 0 to 5">
				{readoutText(summary.rating)}
			</dd>
			<dt>Comments</dt>
			<dd class:mixed={summary.comments.kind === 'mixed'}>{readoutText(summary.comments)}</dd>
		</dl>
		<p class="hint">Use Bulk Edit in the browser to change fields on every selected track.</p>
	{:else if single !== null}
		<header class="ident">
			<div class="art">
				{#if single.artwork_available === true && !artFailed}
					<img
						src={artworkUrl(single.stable_id, 'm')}
						alt=""
						onerror={() => onArtError()}
					/>
				{/if}
			</div>
			<div>
				<h2 data-testid="inspector-title">{single.title ?? '(untitled)'}</h2>
				<p class="sub">{single.artist ?? ''}</p>
			</div>
		</header>
		{#if loadError !== null}
			<p class="error" data-testid="inspector-error">Could not load this track: {loadError}</p>
		{/if}
		{#if saveError !== null}
			<p class="error" data-testid="inspector-save-error" role="alert">Save failed: {saveError}</p>
		{/if}
		<dl class="fields">
			<dt>Rating</dt>
			<dd>
				<RatingStars
					rating={track?.rating ?? single.rating}
					onrate={(next) => void applyPatch({ rating: next })}
					disabledReason={track === null ? "Loading this track's saved version; rating opens once it arrives" : null}
				/>
				{#if sourceOf('rating') !== null}<span class="src" title={sourceTitle('rating')}>{sourceOf('rating')}</span>{/if}
			</dd>
			<dt>BPM</dt>
			<dd title="Analyzed tempo in beats per minute">
				{single.bpm === null ? '' : single.bpm.toFixed(2)}
				{#if sourceOf('bpm') !== null}<span class="src" title={sourceTitle('bpm')}>{sourceOf('bpm')}</span>{/if}
				<button
					class="rb-lit-button tempo"
					data-testid="inspector-tempo-pref"
					title="Set this track's preferred tempo range"
					onclick={(e) => onTempoPref(e.clientX, e.clientY)}
				>Tempo pref</button>
			</dd>
			<dt>Key</dt>
			<dd title="Musical key in Camelot notation">
				{single.key ?? ''}
				{#if sourceOf('key') !== null}<span class="src" title={sourceTitle('key')}>{sourceOf('key')}</span>{/if}
			</dd>
			<dt>Genre</dt>
			<dd>
				{single.genre ?? ''}
				{#if sourceOf('genre') !== null}<span class="src" title={sourceTitle('genre')}>{sourceOf('genre')}</span>{/if}
			</dd>
			<dt>Energy</dt>
			<dd title="Mixed In Key energy level, 1 to 9">
				{single.energy ?? ''}
				{#if sourceOf('energy') !== null}<span class="src" title={sourceTitle('energy')}>{sourceOf('energy')}</span>{/if}
			</dd>
			<dt>Time</dt>
			<dd title="Track length in minutes and seconds">
				{single.duration_ms === null ? '' : formatDuration(single.duration_ms)}
			</dd>
			<dt>Plays</dt>
			<dd title="Times this track has been played">{track?.play_count ?? ''}</dd>
			<dt>Comments</dt>
			<dd>
				{single.comments ?? ''}
				{#if sourceOf('comments') !== null}<span class="src" title={sourceTitle('comments')}>{sourceOf('comments')}</span>{/if}
			</dd>
		</dl>

		<h3>My tags</h3>
		<div class="tags" data-testid="inspector-tags">
			{#each track?.tags ?? [] as tag (tag)}
				<span class="chip">
					{tag}
					<button
						aria-label={`Remove tag ${tag}`}
						title={`Remove tag ${tag}`}
						onclick={() => void applyPatch({ tags_remove: [tag] })}
					>
						<svg viewBox="0 0 24 24" width="9" height="9" aria-hidden="true">
							<path d={CLOSE_PATH} fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" />
						</svg>
					</button>
				</span>
			{/each}
			<input
				placeholder="Add tag"
				aria-label="Add tag"
				disabled={track === null}
				bind:value={newTag}
				onkeydown={(e) => {
					if (e.key === 'Enter') addTag();
				}}
			/>
		</div>

		<h3>Notes</h3>
		<textarea
			rows="3"
			aria-label="Notes"
			disabled={track === null}
			value={track?.notes ?? ''}
			onblur={(e) => {
				const next = e.currentTarget.value;
				if (track !== null && next !== (track.notes ?? '')) void applyPatch({ notes: next });
			}}
		></textarea>

		<h3>In playlists</h3>
		{#if playlistsError !== null}
			<p class="error">Could not list playlists: {playlistsError}</p>
		{:else if playlists === null}
			<p class="dim">Loading...</p>
		{:else if playlists.length === 0}
			<p class="dim">Not in any playlist.</p>
		{:else}
			<ul class="playlists" data-testid="inspector-playlists">
				{#each playlists as hit (hit.playlist_id)}
					<li title={`Position ${hit.positions.join(', ')} in ${hit.name}`}>{hit.name}</li>
				{/each}
			</ul>
		{/if}

		<a class="detail-link" href={`/track/${encodeURIComponent(single.stable_id)}`}>Open full track page</a>
	{/if}
</aside>

<style>
	.inspector {
		grid-area: inspector;
		overflow-y: auto;
		padding: 10px 12px;
		border-left: 1px solid var(--rb-border);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		color: var(--rb-text);
		min-height: 0;
	}
	h2 {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
	}
	h3 {
		margin: 12px 0 4px;
		font-size: var(--rb-fs-label);
		font-weight: 650;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--rb-text-dim);
	}
	.sub,
	.dim,
	.hint {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}
	.hint {
		margin-top: 10px;
	}
	.error {
		color: var(--rb-red);
	}
	.ident {
		display: grid;
		grid-template-columns: 64px minmax(0, 1fr);
		gap: 10px;
		align-items: center;
	}
	.art {
		width: 64px;
		height: 64px;
		background: var(--rb-bg);
		border: 1px solid var(--rb-border);
	}
	.art img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.fields {
		display: grid;
		grid-template-columns: 72px minmax(0, 1fr);
		gap: 5px 8px;
		margin: 12px 0 0;
	}
	.fields dt {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.04em;
		align-self: center;
	}
	.fields dd {
		margin: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		font-variant-numeric: tabular-nums;
	}
	.fields dd.mixed {
		color: var(--rb-text-dim);
		font-style: italic;
	}
	.src {
		font-size: 9px;
		letter-spacing: 0.05em;
		padding: 0 4px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-accent);
	}
	.tempo {
		margin-left: auto;
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		padding: 0 2px 0 6px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 9px;
	}
	.chip button {
		border: 0;
		background: none;
		color: var(--rb-text-dim);
		cursor: pointer;
		font: inherit;
		display: inline-flex;
		align-items: center;
		padding: 0 2px;
	}
	.tags input,
	textarea {
		background: var(--rb-bg);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font: inherit;
		padding: 2px 6px;
	}
	.tags input {
		width: 90px;
	}
	textarea {
		width: 100%;
		box-sizing: border-box;
		resize: vertical;
	}
	.playlists {
		margin: 0;
		padding-left: 14px;
	}
	.detail-link {
		display: inline-block;
		margin-top: 12px;
		color: var(--rb-accent);
	}
</style>
