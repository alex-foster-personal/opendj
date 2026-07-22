<script lang="ts">
	import {
		PlaylistConflictError,
		PlayItError,
		replacePlaylistTracks,
		solvePlayIt,
		type PlayItSolveOut
	} from '$lib/api';
	import { summarizeReorder } from '$lib/play-it-diff';

	let { playlistId }: { playlistId: string } = $props();

	let durationMin = $state(60);
	let peakAtMin = $state<number | null>(null);

	let solving = $state(false);
	let solveError = $state<string | null>(null);
	let result = $state<PlayItSolveOut | null>(null);

	let applying = $state(false);
	let applyError = $state<string | null>(null);
	// Set once Apply succeeds; holds what "Undo" needs to put back.
	let applied = $state<{ previousOrder: string[]; undoEtag: string } | null>(null);

	const summary = $derived(result ? summarizeReorder(result.previous_order, result.proposed_order) : null);

	async function solve(): Promise<void> {
		solving = true;
		solveError = null;
		applied = null;
		try {
			result = await solvePlayIt(playlistId, {
				duration_min: durationMin,
				peak_at_min: peakAtMin
			});
		} catch (err) {
			result = null;
			solveError = err instanceof PlayItError ? err.message : `${err}`;
		} finally {
			solving = false;
		}
	}

	async function apply(): Promise<void> {
		if (!result) return;
		applying = true;
		applyError = null;
		try {
			const { etag } = await replacePlaylistTracks(playlistId, result.proposed_order, result.etag);
			applied = { previousOrder: result.previous_order, undoEtag: etag };
		} catch (err) {
			if (err instanceof PlaylistConflictError) {
				applyError = 'Playlist changed elsewhere since you solved -- re-run PLAY IT and try again.';
				result = null;
			} else {
				applyError = `${err}`;
			}
		} finally {
			applying = false;
		}
	}

	async function undo(): Promise<void> {
		if (!applied) return;
		applying = true;
		applyError = null;
		try {
			await replacePlaylistTracks(playlistId, applied.previousOrder, applied.undoEtag);
			applied = null;
			result = null;
		} catch (err) {
			if (err instanceof PlaylistConflictError) {
				applyError = 'Playlist changed elsewhere since you applied -- cannot undo automatically.';
			} else {
				applyError = `${err}`;
			}
		} finally {
			applying = false;
		}
	}
</script>

<section class="play-it">
	<h3>PLAY IT</h3>
	<form onsubmit={(e) => { e.preventDefault(); solve(); }}>
		<label>
			Duration (min)
			<input type="number" min="30" max="240" bind:value={durationMin} />
		</label>
		<label>
			Peak at (min, optional)
			<input
				type="number"
				min="0"
				value={peakAtMin ?? ''}
				oninput={(e) => {
					const v = (e.currentTarget as HTMLInputElement).value;
					peakAtMin = v === '' ? null : Number(v);
				}}
			/>
		</label>
		<button type="submit" disabled={solving}>{solving ? 'Solving...' : 'Solve order'}</button>
	</form>

	{#if solveError}
		<p class="error">{solveError}</p>
	{/if}

	{#if result}
		{#if summary?.unchanged}
			<p>PLAY IT agrees with the current order -- nothing to apply.</p>
		{:else}
			<p>{summary?.movedCount} of {result.steps.length} tracks would move.</p>
			<ol class="preview">
				{#each result.steps as step (step.position)}
					<li>
						<span class="title">{step.title ?? step.stable_id}</span>
						{#if step.artist}<span class="artist"> -- {step.artist}</span>{/if}
						<span class="hint">{step.transition_hint}</span>
					</li>
				{/each}
			</ol>
			{#if result.constraints_unmet.length > 0}
				<p class="warn">{result.constraints_unmet.length} constraint(s) could not be fully satisfied.</p>
			{/if}
			<button onclick={apply} disabled={applying || applied !== null}>
				{applying ? 'Applying...' : 'Apply order'}
			</button>
		{/if}
	{/if}

	{#if applied}
		<p class="success">Order applied.</p>
		<button onclick={undo} disabled={applying}>{applying ? 'Undoing...' : 'Undo'}</button>
	{/if}

	{#if applyError}
		<p class="error">{applyError}</p>
	{/if}
</section>

<style>
	.play-it {
		border-top: 1px solid var(--border);
		margin-top: 1rem;
		padding-top: 1rem;
	}
	form {
		display: flex;
		gap: 1rem;
		align-items: flex-end;
		flex-wrap: wrap;
	}
	label {
		display: flex;
		flex-direction: column;
		font-size: 0.85rem;
	}
	.preview {
		margin: 0.5rem 0;
		padding-left: 1.5rem;
	}
	.preview .artist {
		color: var(--muted);
	}
	.preview .hint {
		margin-left: 0.5rem;
		font-size: 0.8rem;
		color: var(--muted);
	}
	.error {
		color: var(--danger, #c0392b);
	}
	.warn {
		color: var(--warning, #b8860b);
	}
	.success {
		color: var(--success, #2e7d32);
	}
</style>
