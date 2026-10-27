<script lang="ts">
	/**
	 * AUTOGAIN-03/04: the auto gain readout under a channel's TRIM knob, and
	 * the editor for the user's own per-track value. `AG -3.2 dB` is what the
	 * deck applies; `*` marks the user's own value; `AG N/A` a track with no
	 * loudness analysis (0 dB, nothing guessed). Writes go through the shared
	 * dispatcher (`auto_gain_offset`), so a preset's lifecycle lock refuses
	 * them like any other performance control. API twin:
	 * PUT /api/v1/auto-gain/tracks/{id}/user-offset; CLI twin:
	 * `opendj track auto-gain <id> set <dB>|clear`.
	 */
	import { autoGainReadout, AUTO_GAIN_OFFSET_MAX_DB, AUTO_GAIN_OFFSET_MIN_DB } from '$lib/rb/auto-gain';
	import { deckAutoGain, type DeckId } from '$lib/rb/auto-gain.svelte';

	let {
		deckId,
		onoffset
	}: {
		deckId: DeckId;
		/** The Mixer's `auto_gain_offset` dispatch (performance-ipc). */
		onoffset: (offsetDb: number | null, stableId: string) => Promise<{ ok: boolean; error?: unknown }>;
	} = $props();

	const current = $derived(deckAutoGain[deckId]);
	const readout = $derived(autoGainReadout(current));
	let editing = $state(false);
	/** The track the open editor belongs to: a new load on this deck closes it. */
	let editingFor = $state<string | null>(null);
	const loadedId = $derived(current.state === 'ready' ? current.value.stable_id : null);
	$effect(() => {
		if (editing && loadedId !== editingFor) {
			editing = false;
			editingFor = null;
		}
	});
	let draft = $state<number | null>(null);
	let error = $state<string | null>(null);
	let busy = $state(false);

	function open(event: MouseEvent): void {
		event.stopPropagation();
		if (current.state !== 'ready') return;
		const v = current.value;
		draft = Math.round((v.user_offset_db ?? v.analysed_offset_db ?? 0) * 10) / 10;
		error = null;
		editing = !editing;
		editingFor = editing ? v.stable_id : null;
	}

	async function write(offsetDb: number | null): Promise<void> {
		// The draft belongs to the track the editor was opened for; never
		// write it to whatever is loaded now.
		if (editingFor === null || loadedId !== editingFor) {
			editing = false;
			error = 'The track on this deck changed; open the editor again.';
			return;
		}
		busy = true;
		error = null;
		const result = await onoffset(offsetDb, editingFor);
		busy = false;
		if (result.ok) editing = false;
		else error = result.error instanceof Error ? result.error.message : String(result.error);
	}

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		const value = draft;
		if (value === null || !Number.isFinite(value) || value < AUTO_GAIN_OFFSET_MIN_DB || value > AUTO_GAIN_OFFSET_MAX_DB) {
			error = `Enter a number of dB from ${AUTO_GAIN_OFFSET_MIN_DB} to ${AUTO_GAIN_OFFSET_MAX_DB}.`;
			return;
		}
		void write(value);
	}
</script>

<div class="ag" data-testid={`auto-gain-deck-${deckId}`}>
	<button
		type="button"
		class={`ag-readout tone-${readout.tone}`}
		title={readout.title}
		aria-label={`auto gain deck ${deckId}: ${readout.title}`}
		aria-expanded={editing}
		disabled={current.state !== 'ready'}
		onclick={open}>{readout.label}</button
	>
	{#if editing && current.state === 'ready'}
		<form class="ag-editor" onsubmit={submit}>
			<label>
				<span>Your offset (dB)</span>
				<input
					type="number"
					step="0.1"
					min={AUTO_GAIN_OFFSET_MIN_DB}
					max={AUTO_GAIN_OFFSET_MAX_DB}
					bind:value={draft}
					aria-label={`auto gain user offset deck ${deckId}`}
				/>
			</label>
			<div class="ag-actions">
				<button type="submit" disabled={busy}>Set</button>
				<button
					type="button"
					disabled={busy || current.value.user_offset_db === null}
					title="Drop your value and use the analysed one"
					onclick={() => void write(null)}>Use analysed</button
				>
				<button type="button" onclick={() => (editing = false)}>Close</button>
			</div>
			{#if error}<p class="ag-error" role="alert">{error}</p>{/if}
		</form>
	{/if}
</div>

<style>
	/* Overlaid, not stacked: the strip's height floor is measured
	 * (channel-strip-less-floor.test.mjs), so the readout takes no row. */
	.ag {
		position: absolute;
		left: 50%;
		bottom: -0.55rem;
		transform: translateX(-50%);
		display: flex;
		justify-content: center;
		z-index: 2;
	}
	.ag-readout {
		font-size: 0.6rem;
		line-height: 1.1;
		padding: 0 0.2rem;
		border: none;
		background: transparent;
		color: var(--muted);
		cursor: pointer;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.ag-readout:disabled {
		cursor: default;
	}
	.tone-normal {
		color: var(--fg);
	}
	.tone-user {
		color: var(--accent);
	}
	.tone-warn {
		color: var(--warning);
	}
	.ag-editor {
		position: absolute;
		top: 100%;
		z-index: 20;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding: 0.4rem;
		min-width: 9rem;
		background: var(--surface-raised);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 4px;
		font-size: 0.7rem;
	}
	.ag-editor label {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.ag-editor input {
		width: 100%;
	}
	.ag-actions {
		display: flex;
		gap: 0.25rem;
		flex-wrap: wrap;
	}
	.ag-error {
		margin: 0;
		color: var(--warning);
	}
</style>
