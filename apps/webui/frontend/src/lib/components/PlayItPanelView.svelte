<script lang="ts">
	// Props-only half of PlayItPanel: the PLAY IT goal form, the proposed
	// order and its apply/undo. The container loads the playlist and makes
	// every request; the goal fields are bindable so it reads what was typed.
	import type { components } from '$lib/api-types';
	import { formatUnscoredEdges, summarizeReorder } from '$lib/play-it-diff';

	type PlayItSolveOut = components['schemas']['PlayItSolveOut'];
	type PlaylistTrack = components['schemas']['PlaylistDetail']['tracks'][number];

	let {
		durationMin = $bindable(),
		peakAtMin = $bindable(),
		peakPins = $bindable(),
		openerPins = $bindable(),
		closerPin = $bindable(),
		playlistTracks,
		solving,
		solveError,
		result,
		applying,
		applyError,
		applied,
		solve,
		apply,
		undo
	}: {
		durationMin: number;
		peakAtMin: number | null;
		peakPins: string[];
		openerPins: string[];
		closerPin: string;
		playlistTracks: PlaylistTrack[];
		solving: boolean;
		solveError: string | null;
		result: PlayItSolveOut | null;
		applying: boolean;
		applyError: string | null;
		applied: { previousOrder: string[]; undoEtag: string } | null;
		solve: () => unknown;
		apply: () => unknown;
		undo: () => unknown;
	} = $props();

	const summary = $derived(
		result ? summarizeReorder(result.previous_order, result.proposed_order) : null
	);

	const unscoredHeader = $derived.by(() => {
		if (!result || (result.unscored_edges ?? 0) <= 0) {
			return null;
		}
		const terms = result.steps.flatMap((step) => step.unscored ?? []);
		return formatUnscoredEdges(result.unscored_edges, Math.max(0, result.steps.length - 1), terms);
	});

	function trackLabel(track: PlaylistTrack): string {
		const title = track.title ?? track.stable_id;
		return track.artist ? `${title} -- ${track.artist}` : title;
	}

	function togglePin(list: string[], stableId: string, checked: boolean): string[] {
		if (checked) {
			return list.includes(stableId) ? list : [...list, stableId];
		}
		return list.filter((id) => id !== stableId);
	}
</script>

<section class="play-it">
	<h3>PLAY IT</h3>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			solve();
		}}
	>
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
		{#if unscoredHeader}
			<p class="warn">{unscoredHeader}</p>
		{/if}
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
				<p class="warn">
					{result.constraints_unmet.length} constraint(s) could not be fully satisfied.
				</p>
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
