<script lang="ts">
	// bulk-edit (issue #178): set rating / notes / tags across every
	// selected track in one atomic request (backend pre-checks every row's
	// ETag before writing any of them - see routes/bulk_edit.py).
	import { bulkEditTracks, editSuiteConflictRows } from '$lib/rb/api-edit-suite';
	import {
		MIXED_READOUT,
		bulkEditFieldValues,
		bulkEditPayload,
		initialFieldText,
		isApplyChord,
		type BulkEditRowFields
	} from '$lib/rb/bulk-edit-values';
	import { pushToast } from '$lib/stores.svelte';
	import EditSuiteModal from './EditSuiteModal.svelte';

	let {
		stableIds,
		etags,
		rows,
		onclose,
		onapplied
	}: {
		stableIds: string[];
		etags: Record<string, string>;
		rows: BulkEditRowFields[];
		onclose: () => void;
		onapplied: () => void;
	} = $props();

	// LIBM-63: a mixed field is a read-only "Multiple" readout until its Set box is ticked;
	// the readout text is never an input value, and an unticked field is left out of the PATCH.
	const {
		rating: ratingConsensus,
		notes: notesConsensus,
		comments: commentsConsensus
	} = bulkEditFieldValues(stableIds, rows);

	let setRating = $state(false);
	let ratingText = $state(
		initialFieldText(ratingConsensus, (value) => (value === null ? '' : String(value)))
	);
	let setNotes = $state(false);
	let notes = $state(initialFieldText(notesConsensus, (value) => value));
	let setGenre = $state(false);
	let genre = $state('');
	let setComments = $state(false);
	let comments = $state(initialFieldText(commentsConsensus, (value) => value));
	let tagsAdd = $state('');
	let tagsRemove = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	// LIBM-64: Ctrl+Enter (Cmd+Enter) in a field runs the one apply path.
	function onFieldKeydown(event: KeyboardEvent): void {
		if (!isApplyChord(event)) return;
		event.preventDefault();
		event.stopPropagation();
		void apply();
	}

	async function apply(): Promise<void> {
		if (busy) return;
		const result = bulkEditPayload({
			stableIds,
			etags,
			rating: { set: setRating, text: String(ratingText ?? '') },
			notes: { set: setNotes, text: notes },
			genre: { set: setGenre, text: genre },
			comments: { set: setComments, text: comments },
			tagsAdd,
			tagsRemove
		});
		if (!result.ok) {
			error = result.error;
			return;
		}
		busy = true;
		error = null;
		try {
			const res = await bulkEditTracks(result.patch);
			pushToast(`bulk edit applied to ${res.applied_count} track(s)`, 'info');
			onapplied();
		} catch (exc) {
			// STATE-07: the 409 body names exactly which rows failed their ETag
			// precondition - surface that instead of a bare "conflict: Conflict".
			const conflicts = editSuiteConflictRows(exc);
			error =
				conflicts !== null
					? `${conflicts.length} of ${stableIds.length} track(s) changed elsewhere since this selection was made - reload and try again`
					: String(exc);
			pushToast(`bulk edit failed: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<EditSuiteModal title={`Bulk Edit - ${stableIds.length} track(s)`} {onclose}>
	<div class="be-form">
		<label class="be-field">
			<input type="checkbox" bind:checked={setRating} />
			Rating
			{#if ratingConsensus.kind === 'mixed' && !setRating}
				<span class="be-mixed-readout">{MIXED_READOUT}</span>
			{:else}
				<input
					type="number"
					min="0"
					max="5"
					bind:value={ratingText}
					disabled={!setRating}
					onkeydown={onFieldKeydown}
				/>
			{/if}
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setNotes} />
			Notes
			{#if notesConsensus.kind === 'mixed' && !setNotes}
				<span class="be-mixed-readout">{MIXED_READOUT}</span>
			{:else}
				<input
					type="text"
					bind:value={notes}
					disabled={!setNotes}
					placeholder="new notes text"
					onkeydown={onFieldKeydown}
				/>
			{/if}
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setGenre} />
			Genre
			<input
				type="text"
				bind:value={genre}
				disabled={!setGenre}
				placeholder="new genre text"
				onkeydown={onFieldKeydown}
			/>
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setComments} />
			Comments
			{#if commentsConsensus.kind === 'mixed' && !setComments}
				<span class="be-mixed-readout">{MIXED_READOUT}</span>
			{:else}
				<input
					type="text"
					bind:value={comments}
					disabled={!setComments}
					placeholder="new comments text"
					onkeydown={onFieldKeydown}
				/>
			{/if}
		</label>
		<label class="be-field">
			Add tags
			<input
				type="text"
				bind:value={tagsAdd}
				placeholder="comma,separated,tags"
				onkeydown={onFieldKeydown}
			/>
		</label>
		<label class="be-field">
			Remove tags
			<input
				type="text"
				bind:value={tagsRemove}
				placeholder="comma,separated,tags"
				onkeydown={onFieldKeydown}
			/>
		</label>
	</div>

	{#if error !== null}
		<p class="be-error">{error}</p>
	{/if}

	<div class="be-actions">
		<button class="rb-lit-button" onclick={onclose} disabled={busy}>Cancel</button>
		<button class="rb-lit-button primary" onclick={apply} disabled={busy}>Apply</button>
	</div>
</EditSuiteModal>

<style>
	.be-form {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.be-field {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	.be-field input[type='text'],
	.be-field input[type='number'] {
		flex: 1;
		background: var(--rb-bg, #14161a);
		border: 1px solid var(--rb-border, #333);
		color: var(--rb-text, #ddd);
		padding: 4px 6px;
		border-radius: 2px;
	}
	.be-mixed-readout {
		flex: 1;
		font-style: italic;
		color: var(--rb-text-dim);
	}
	.be-error {
		color: var(--rb-red, #e55);
		font-size: 11px;
	}
	.be-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 14px;
	}
</style>
