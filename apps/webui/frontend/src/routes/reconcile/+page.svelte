<script lang="ts">
	import { onMount } from 'svelte';
	import { getTrack } from '$lib/api';
	import {
		applyRelocate,
		getRelocateCandidates,
		listBroken,
		RelocateApplyError,
		type BrokenTrack,
		type RelocateCandidate
	} from '$lib/reconcile-api';
	import { pushToast } from '$lib/stores.svelte';

	let broken = $state<BrokenTrack[]>([]);
	let loading = $state(true);
	let expanded = $state<string | null>(null);
	let candidates = $state<RelocateCandidate[]>([]);
	let candidateOriginalPath = $state<string | null>(null);
	let candidatesLoading = $state(false);
	let applying = $state<string | null>(null);

	async function load(): Promise<void> {
		loading = true;
		try {
			const page = await listBroken();
			broken = page.tracks;
		} catch (exc) {
			pushToast(`Failed to load broken tracks: ${exc}`, 'error');
		} finally {
			loading = false;
		}
	}

	async function toggle(track: BrokenTrack): Promise<void> {
		if (expanded === track.stable_id) {
			expanded = null;
			candidates = [];
			candidateOriginalPath = null;
			return;
		}
		expanded = track.stable_id;
		candidates = [];
		candidatesLoading = true;
		try {
			const found = await getRelocateCandidates(track.stable_id);
			candidates = found.candidates;
			candidateOriginalPath = found.original_path;
		} catch (exc) {
			pushToast(`Failed to find candidates: ${exc}`, 'error');
		} finally {
			candidatesLoading = false;
		}
	}

	async function relocate(track: BrokenTrack, candidate: RelocateCandidate): Promise<void> {
		applying = track.stable_id;
		try {
			if (!candidateOriginalPath) {
				throw new Error('candidate selection is missing its recorded path');
			}
			const { etag } = await getTrack(track.stable_id);
			await applyRelocate(track.stable_id, candidate.path, {
				ifMatch: etag,
				expectedOriginalPath: candidateOriginalPath
			});
			pushToast(`Relocated "${track.title ?? track.stable_id}"`, 'info');
			expanded = null;
			candidates = [];
			candidateOriginalPath = null;
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

	onMount(load);
</script>

<h2>Missing tracks</h2>
<p style="color: var(--muted);">
	Local tracks whose recorded path no longer resolves on disk. Streaming
	tracks and pathless rows never appear here.
</p>

{#if loading}
	<p style="color: var(--muted); margin-top: 1rem;">Loading...</p>
{:else if broken.length === 0}
	<p style="color: var(--muted); margin-top: 1rem;">No broken tracks. Library is clean.</p>
{:else}
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
					<td>
						<button onclick={() => toggle(track)}>
							{expanded === track.stable_id ? 'Hide' : 'Relocate'}
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
												disabled={applying === track.stable_id}
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
</style>
