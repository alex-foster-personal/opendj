<script lang="ts">
	import { shownKey } from '$lib/rb/key-notation';
	// LIBWS-02: the Crate Desk inspector. One selected track shows its fields
	// with who set each one (provenance source chip) and edits rating, tags
	// and notes through the same If-Match PATCH the track page uses. Several
	// selected tracks show a summary where a field the rows disagree on reads
	// "Multiple values", never one row's value standing in for all.
	import { ConflictError, getTrack, patchTrack, type Track } from '$lib/api';
	import ConflictDialog from '$lib/components/ConflictDialog.svelte';
	import TempoPrefPopover from '$lib/components/TempoPrefPopover.svelte';
	import RatingStars from '$lib/components/rb/browser/RatingStars.svelte';
	import { artworkUrl } from '$lib/rb/artwork-url';
	import { CLOSE_PATH } from '$lib/ui/icon-glyphs';
	import { listTrackPlaylists, type TrackPlaylistHit } from '$lib/rb/track-playlists';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import {
		formatBpmRange,
		formatDuration,
		readoutText,
		sourceLabel,
		summarizeSelection
	} from '$lib/library/inspector-summary';

	type Patch = { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string };

	const rows = $derived(libraryWorkspace.selected_rows);
	const single = $derived(rows.length === 1 ? rows[0] : null);
	const summary = $derived(rows.length > 1 ? summarizeSelection(rows) : null);
	// Keyed on the id, not the row object: the browser republishes fresh row
	// objects for the same track (a pane refresh after a save), and the load
	// effect below must not reset the inspector, or close an open Tempo pref
	// editor, when the selected track has not changed.
	const singleId = $derived(single?.stable_id ?? null);

	let track = $state<Track | null>(null);
	let etag = $state('');
	let loadError = $state<string | null>(null);
	let playlists = $state<TrackPlaylistHit[] | null>(null);
	let playlistsError = $state<string | null>(null);
	let pendingPatch = $state<Patch | null>(null);
	let conflictServer = $state<Track | null>(null);
	let newTag = $state('');
	/** Why the last save failed, shown inline until the next save or selection. */
	let saveError = $state<string | null>(null);
	let tempoAnchor = $state<{ x: number; y: number } | null>(null);
	let artFailed = $state(false);

	let loadSeq = 0;
	$effect(() => {
		const sid = singleId;
		track = null;
		etag = '';
		loadError = null;
		saveError = null;
		// A conflict belongs to the track it was raised on; keeping it across a
		// selection change would let "Take theirs" or "Keep mine" act on that
		// track while another one is showing.
		conflictServer = null;
		pendingPatch = null;
		settleConflict(null);
		// Every selection change starts a new load, even to none or several
		// tracks, so a save still in flight for the old track never lands here.
		const seq = ++loadSeq;
		playlists = null;
		playlistsError = null;
		tempoAnchor = null;
		artFailed = false;
		if (sid === null) return;
		getTrack(sid).then(
			(res) => {
				if (seq !== loadSeq) return;
				track = res.track;
				etag = res.etag;
			},
			(exc: unknown) => {
				if (seq === loadSeq) loadError = String(exc);
			}
		);
		listTrackPlaylists(sid).then(
			(hits) => {
				if (seq === loadSeq) playlists = hits;
			},
			(exc: unknown) => {
				if (seq === loadSeq) playlistsError = String(exc);
			}
		);
	});

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

	// A 409 holds the save queue until the user answers the dialog, so a save
	// queued behind it never runs on the conflict etag or takes over its
	// pendingPatch. The answer is the patch to send next (null sends nothing).
	let answerConflict: ((next: Patch | null) => void) | null = null;
	function settleConflict(next: Patch | null): void {
		const answer = answerConflict;
		answerConflict = null;
		answer?.(next);
	}

	// Saves run one at a time: each PATCH needs the etag the previous one
	// returned, and each owns its pendingPatch until it settles. A save queued
	// for a track the selection has since left is dropped.
	let saveChain: Promise<void> = Promise.resolve();
	function applyPatch(patch: Patch): Promise<void> {
		const seq = loadSeq;
		const run = saveChain.then(() => (seq === loadSeq ? savePatch(patch) : undefined));
		saveChain = run.catch(() => {});
		return run;
	}

	async function savePatch(patch: Patch): Promise<void> {
		if (track === null) return;
		// The save is for THIS track. The selection can move while it is in
		// flight, so the answer must never land on whatever is selected by then:
		// the row is captured now, and the inspector's own state is written only
		// if the same load is still showing.
		const seq = loadSeq;
		const row = single !== null && single.stable_id === track.stable_id ? single : null;
		pendingPatch = patch;
		saveError = null;
		try {
			const res = await patchTrack(track.stable_id, etag, patch);
			// Keep the browser row in step so the table shows the same value.
			// Rating is the only field this inspector saves that the row carries:
			// tags have no browser column, and notes are not the Comments
			// column (that is the separate `comments` field).
			if (row !== null && patch.rating !== undefined) row.rating = res.track.rating ?? null;
			if (row !== null && row.etag !== '') row.etag = res.etag;
			if (seq !== loadSeq) return;
			track = res.track;
			etag = res.etag;
			pendingPatch = null;
		} catch (exc) {
			if (seq !== loadSeq) return;
			if (exc instanceof ConflictError) {
				conflictServer = exc.current;
				etag = exc.etag;
				const next = await new Promise<Patch | null>((resolve) => {
					answerConflict = resolve;
				});
				if (next !== null && seq === loadSeq) await savePatch(next);
			} else {
				saveError = String(exc);
			}
		}
	}

	function keepMine(): void {
		const patch = pendingPatch;
		conflictServer = null;
		settleConflict(patch);
	}

	function takeTheirs(): void {
		if (conflictServer !== null) track = conflictServer;
		conflictServer = null;
		pendingPatch = null;
		settleConflict(null);
	}

	function mergeTags(): void {
		if (conflictServer === null || pendingPatch?.tags_add === undefined) {
			takeTheirs();
			return;
		}
		const merged = Array.from(new Set([...conflictServer.tags, ...pendingPatch.tags_add]));
		conflictServer = null;
		settleConflict({ tags_add: merged });
	}

	function addTag(): void {
		const tag = newTag.trim();
		if (tag === '') return;
		newTag = '';
		void applyPatch({ tags_add: [tag] });
	}
</script>

<aside class="inspector rb-panel" data-testid="track-inspector">
	{#if rows.length === 0}
		<p class="hint" data-testid="inspector-empty">Select tracks in the browser to inspect and edit them.</p>
	{:else if summary !== null}
		<header>
			<h2 data-testid="inspector-multi-heading">{summary.count} tracks selected</h2>
			<p class="sub">{libraryWorkspace.pane_title}</p>
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
						onerror={() => (artFailed = true)}
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
					onclick={(e) => (tempoAnchor = { x: e.clientX, y: e.clientY })}
				>Tempo pref</button>
			</dd>
			<dt>Key</dt>
			<dd title="Musical key, in the key notation chosen in Settings">
				{shownKey(single.key) ?? ''}
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

{#if tempoAnchor !== null && single !== null}
	<TempoPrefPopover
		stableId={single.stable_id}
		x={tempoAnchor.x}
		y={tempoAnchor.y}
		onclose={() => (tempoAnchor = null)}
	/>
{/if}

{#if conflictServer !== null}
	<ConflictDialog
		mine={pendingPatch ?? {}}
		current={conflictServer}
		onkeep_mine={keepMine}
		ontake_theirs={takeTheirs}
		onmerge_tags={mergeTags}
	/>
{/if}

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
