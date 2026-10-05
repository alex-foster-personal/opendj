<script lang="ts">
	import { onMount } from 'svelte';
	import { getTrack } from '$lib/api';
	import {
		applyRelocate,
		getRelocateCandidates,
		listBrokenPage,
		RelocateApplyError,
		type BrokenTrack,
		type RelocateCandidate
	} from '$lib/reconcile-api';
	import {
		rekordboxWriteback,
		rekordboxWritebackRefusal
	} from '$lib/rb/rekordbox-writeback.svelte';
	import {
		MISSING_PAGE_SIZE,
		loadBrokenRows,
		missingCountLabel,
		missingCountTitle,
		missingMoreLabel,
		missingView
	} from '$lib/reconcile-paging';
	import { removeFromLibrary } from '$lib/rb/track-library';
	import {
		removeFromLibraryConfirmMessage,
		removeFromLibraryToastMessage
	} from '$lib/components/rb/browser/track-library-menu';
	import { pushToast } from '$lib/stores.svelte';

	let broken = $state<BrokenTrack[]>([]);
	let loading = $state(true);
	// The whole count of missing rows, which is not `broken.length`: the page
	// holds one page at a time. `nextOffset` is null once every row is loaded.
	let total = $state(0);
	let nextOffset = $state<number | null>(null);
	let loadError = $state<string | null>(null);
	let measuredAt = $state<Date | null>(null);
	const view = $derived(missingView({ loading, error: loadError, loaded: broken.length }));
	let expanded = $state<string | null>(null);
	let candidates = $state<RelocateCandidate[]>([]);
	let candidateOriginalPath = $state<string | null>(null);
	let candidateVendorId = $state<string | null>(null);
	let candidatesLoading = $state(false);
	let applying = $state<string | null>(null);
	let removing = $state<string | null>(null);

	// Relocate apply patches djmdContent.FolderPath on the LIVE rekordbox
	// database, so it is inert whenever the daemon is in one-way import mode.
	// ONE function gates the request and supplies the tooltip, so the disabled
	// button can never disagree with the reason it is disabled.
	const writebackRefusal = $derived(rekordboxWritebackRefusal());

	/**
	 * Load from the top. The first load asks for one page; a reload after a
	 * relocate or a removal asks for as many rows as were already on screen,
	 * so an edit never collapses what the user had paged in.
	 */
	async function load(): Promise<void> {
		loading = true;
		try {
			const got = await loadBrokenRows(
				listBrokenPage,
				Math.max(MISSING_PAGE_SIZE, broken.length)
			);
			broken = got.tracks;
			total = got.total;
			nextOffset = got.nextOffset;
			measuredAt = new Date();
			loadError = null;
		} catch (exc) {
			loadError = String(exc);
			console.error('[reconcile] loading missing tracks failed', exc);
			pushToast(`Failed to load missing tracks: ${exc}`, 'error');
		} finally {
			loading = false;
		}
	}

	async function loadMore(): Promise<void> {
		if (nextOffset === null || loading) return;
		loading = true;
		try {
			const got = await loadBrokenRows(listBrokenPage, MISSING_PAGE_SIZE, nextOffset);
			// The listing can shift between requests (a file comes back, a row
			// is removed elsewhere), so a row may arrive twice. Keyed rows must
			// be unique, and the first copy is the one already on screen.
			const seen = new Set(broken.map((t) => t.stable_id));
			broken = [...broken, ...got.tracks.filter((t) => !seen.has(t.stable_id))];
			total = got.total;
			nextOffset = got.nextOffset;
			measuredAt = new Date();
			loadError = null;
		} catch (exc) {
			loadError = String(exc);
			console.error('[reconcile] loading more missing tracks failed', exc);
			pushToast(`Failed to load more missing tracks: ${exc}`, 'error');
		} finally {
			loading = false;
		}
	}

	async function toggle(track: BrokenTrack): Promise<void> {
		if (expanded === track.stable_id) {
			expanded = null;
			candidates = [];
			candidateOriginalPath = null;
			candidateVendorId = null;
			return;
		}
		expanded = track.stable_id;
		candidates = [];
		candidatesLoading = true;
		try {
			const found = await getRelocateCandidates(track.stable_id);
			candidates = found.candidates;
			candidateOriginalPath = found.original_path;
			candidateVendorId = found.vendor_id;
		} catch (exc) {
			pushToast(`Failed to find candidates: ${exc}`, 'error');
		} finally {
			candidatesLoading = false;
		}
	}

	async function relocate(track: BrokenTrack, candidate: RelocateCandidate): Promise<void> {
		if (writebackRefusal !== null) return;
		applying = track.stable_id;
		try {
			if (!candidateOriginalPath) {
				throw new Error('candidate selection is missing its recorded path');
			}
			const { etag } = await getTrack(track.stable_id);
			await applyRelocate(track.stable_id, candidate.path, {
				ifMatch: etag,
				expectedOriginalPath: candidateOriginalPath,
				expectedVendorId: candidateVendorId,
				expectedCandidateIdentity: candidate.identity_token
			});
			pushToast(`Relocated "${track.title ?? track.stable_id}"`, 'info');
			expanded = null;
			candidates = [];
			candidateOriginalPath = null;
			candidateVendorId = null;
			await load();
		} catch (exc) {
			if (exc instanceof RelocateApplyError) {
				pushToast(`Relocate failed (${exc.code}): ${exc.message}`, 'error');
			} else {
				pushToast(`Relocate failed: ${exc}`, 'error');
			}
		} finally {
			applying = null;
		}
	}

	async function removeFromLibraryRow(track: BrokenTrack): Promise<void> {
		if (!window.confirm(removeFromLibraryConfirmMessage(1))) return;
		removing = track.stable_id;
		try {
			await removeFromLibrary(track.stable_id);
			pushToast(removeFromLibraryToastMessage(1), 'info');
			if (expanded === track.stable_id) expanded = null;
			await load();
		} catch (exc) {
			pushToast(`remove from library failed: ${String(exc)}`, 'error');
		} finally {
			removing = null;
		}
	}

	onMount(() => {
		void rekordboxWriteback.probe();
		void load();
	});
</script>

<h2>Missing tracks</h2>
<p style="color: var(--muted);">
	Local tracks whose recorded path no longer resolves on disk. Streaming
	tracks and pathless rows never appear here. Remove from library drops the
	track from OpenDJ only; the file stays on disk.
</p>

{#if view === 'loading'}
	<p style="color: var(--muted); margin-top: 1rem;" data-testid="missing-loading">
		Checking the first {MISSING_PAGE_SIZE} recorded paths...
	</p>
{:else if view === 'error'}
	<p style="color: var(--danger); margin-top: 1rem;" data-testid="missing-error">
		Could not load missing tracks, so nothing is known about the library yet: {loadError}
		<button onclick={() => void load()}>Retry</button>
	</p>
{:else if view === 'empty'}
	<p style="color: var(--muted); margin-top: 1rem;" data-testid="missing-empty">
		No missing tracks. Every recorded local path resolves on this machine.
	</p>
{:else}
	<p style="color: var(--muted); margin-top: 1rem;" data-testid="missing-count">
		<span title={measuredAt ? missingCountTitle(total, measuredAt) : undefined}>
			{missingCountLabel(broken.length, total)}
		</span>
		{#if loading}
			<span>- loading...</span>
		{:else if loadError !== null}
			<span style="color: var(--danger);">
				- the last load failed ({loadError}); the rows below are from before it.
			</span>
			<button onclick={() => void load()}>Retry</button>
		{/if}
	</p>
	<table class="library" style="margin-top: 1rem;">
		<thead>
			<tr>
				<th>Title</th>
				<th>Artist</th>
				<th>Original path</th>
				<th></th>
			</tr>
		</thead>
		<tbody>
			{#each broken as track (track.stable_id)}
				<tr>
					<td><a href={`/track/${track.stable_id}`}>{track.title ?? track.stable_id}</a></td>
					<td>{track.artist ?? ''}</td>
					<td><code style="font-size: 0.8rem;">{track.original_path}</code></td>
					<td class="actions">
						<button onclick={() => toggle(track)}>
							{expanded === track.stable_id ? 'Hide' : 'Relocate'}
						</button>
						<button
							disabled={removing === track.stable_id}
							title="Remove from library (file stays on disk)"
							onclick={() => void removeFromLibraryRow(track)}
						>
							{removing === track.stable_id ? 'Removing...' : 'Remove from library'}
						</button>
					</td>
				</tr>
				{#if expanded === track.stable_id}
					<tr>
						<td colspan="4">
							{#if candidatesLoading}
								<p style="color: var(--muted);">Searching for candidates...</p>
							{:else if candidates.length === 0}
								<p style="color: var(--muted);">
									No candidates found under the configured music roots.
								</p>
							{:else}
								<ul class="candidate-list">
									{#each candidates as cand}
										<li>
											<code style="font-size: 0.8rem;">{cand.path}</code>
											<span style="color: var(--muted);">
												({(cand.confidence * 100).toFixed(0)}%{cand.triple_validated
													? ', triple-validated'
													: ''})
											</span>
											<button
												disabled={applying === track.stable_id || writebackRefusal !== null}
												title={writebackRefusal ?? 'Point this track at the chosen file'}
												onclick={() => relocate(track, cand)}
											>
												{applying === track.stable_id ? 'Applying...' : 'Use this file'}
											</button>
										</li>
									{/each}
								</ul>
							{/if}
						</td>
					</tr>
				{/if}
			{/each}
		</tbody>
	</table>
	{#if nextOffset !== null}
		<p style="margin-top: 0.75rem;">
			<button
				onclick={() => void loadMore()}
				disabled={loading}
				data-testid="missing-more"
				title={`Loads the next ${MISSING_PAGE_SIZE} missing tracks. ${broken.length.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} are loaded.`}
			>
				{loading ? 'Loading...' : missingMoreLabel(broken.length, total)}
			</button>
		</p>
	{/if}
{/if}

<style>
	.candidate-list {
		list-style: none;
		margin: 0.4rem 0;
		padding: 0;
	}
	.candidate-list li {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.3rem 0;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}
</style>
