<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getPlaylist,
		PlaylistConflictError,
		PlayItError,
		replacePlaylistTracks,
		solvePlayIt,
		type PlayItSolveOut,
		type PlaylistDetail
	} from '$lib/api';
	import { publishSetGoalPins } from '$lib/rb/autoplay-queue.svelte';
	import { summarizeReorder } from '$lib/play-it-diff';

	let { playlistId }: { playlistId: string } = $props();

	let durationMin = $state(60);
	let peakAtMin = $state<number | null>(null);
	let playlistTracks = $state<PlaylistDetail['tracks']>([]);
	let peakPins = $state<string[]>([]);
	let openerPins = $state<string[]>([]);
	let closerPin = $state<string>('');

	let solving = $state(false);
	let solveError = $state<string | null>(null);
	let result = $state<PlayItSolveOut | null>(null);

	let applying = $state(false);
	let applyError = $state<string | null>(null);
	let applied = $state<{ previousOrder: string[]; undoEtag: string } | null>(null);

	const summary = $derived(result ? summarizeReorder(result.previous_order, result.proposed_order) : null);

	onMount(async () => {
		const detail = await getPlaylist(playlistId);
		playlistTracks = detail.tracks;
	});

	function trackLabel(track: PlaylistDetail['tracks'][number]): string {
		const title = track.title ?? track.stable_id;
		return track.artist ? `${title} -- ${track.artist}` : title;
	}

	function togglePin(list: string[], stableId: string, checked: boolean): string[] {
		if (checked) {
			return list.includes(stableId) ? list : [...list, stableId];
		}
		return list.filter((id) => id !== stableId);
	}

	function publishPinsFromResult(solve: PlayItSolveOut): void {
		const roleOf = new Map<string, 'opener' | 'peak' | 'closer'>();
		for (const step of solve.steps) {
			if (step.pin_role) {
				roleOf.set(step.stable_id, step.pin_role);
			}
		}
		publishSetGoalPins(roleOf);
	}

	async function solve(): Promise<void> {
		solving = true;
		solveError = null;
		applied = null;
		try {
			result = await solvePlayIt(playlistId, {
				duration_min: durationMin,
				peak_at_min: peakAtMin,
				peak_pins: peakPins,
				opener_pins: openerPins,
				closer_pin: closerPin || null
			});
			publishPinsFromResult(result);
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
		<fieldset class="pin-fieldset">
			<legend>Peak pins</legend>
			{#each playlistTracks as track (track.stable_id)}
				<label class="pin-option">
					<input
						type="checkbox"
						checked={peakPins.includes(track.stable_id)}
						onchange={(e) => {
							peakPins = togglePin(
								peakPins,
								track.stable_id,
								(e.currentTarget as HTMLInputElement).checked
							);
						}}
					/>
					{trackLabel(track)}
				</label>
			{/each}
		</fieldset>
		<fieldset class="pin-fieldset">
			<legend>Viable openers</legend>
			{#each playlistTracks as track (track.stable_id)}
				<label class="pin-option">
					<input
						type="checkbox"
						checked={openerPins.includes(track.stable_id)}
						onchange={(e) => {
							openerPins = togglePin(
								openerPins,
								track.stable_id,
								(e.currentTarget as HTMLInputElement).checked
							);
						}}
					/>
					{trackLabel(track)}
				</label>
			{/each}
		</fieldset>
		<label>
			Closer
			<select bind:value={closerPin}>
				<option value="">none</option>
				{#each playlistTracks as track (track.stable_id)}
					<option value={track.stable_id}>{trackLabel(track)}</option>
				{/each}
			</select>
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
						{#if step.pin_role}<span class="role">{step.pin_role}</span>{/if}
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
	.pin-fieldset {
		border: 1px solid var(--border);
		padding: 0.5rem;
		max-height: 8rem;
		overflow: auto;
	}
	.pin-option {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		font-size: 0.8rem;
	}
	.preview {
		margin: 0.5rem 0;
		padding-left: 1.5rem;
	}
	.preview .artist {
		color: var(--muted);
	}
	.preview .role {
		margin-left: 0.35rem;
		font-size: 0.75rem;
		color: var(--rb-accent, var(--accent));
		text-transform: lowercase;
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
