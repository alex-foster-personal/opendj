<script lang="ts">
	// bulk-edit (issue #178): set rating / notes / tags across every
	// selected track in one atomic request (backend pre-checks every row's
	// ETag before writing any of them - see routes/bulk_edit.py).
	import { bulkEditTracks } from '$lib/rb/api-edit-suite';
	import {
		MIXED_READOUT,
		bulkEditFieldValues,
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

	const { rating: ratingConsensus, notes: notesConsensus } = bulkEditFieldValues(
		stableIds,
		rows
	);

	function _ratingToText(value: number | null): string {
		return value === null ? '' : String(value);
	}

	let setRating = $state(false);
	let ratingText = $state(
		ratingConsensus.kind === 'shared' ? _ratingToText(ratingConsensus.value) : ''
	);
	let setNotes = $state(false);
	let notes = $state(notesConsensus.kind === 'shared' ? notesConsensus.value : '');
	let tagsAdd = $state('');
	let tagsRemove = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	function onSetRatingToggle(checked: boolean): void {
		setRating = checked;
		if (ratingConsensus.kind === 'mixed') {
			ratingText = '';
		}
	}

	function onSetNotesToggle(checked: boolean): void {
		setNotes = checked;
		if (notesConsensus.kind === 'mixed') {
			notes = '';
		}
	}

	function _splitTags(raw: string): string[] {
		return raw
			.split(',')
			.map((t) => t.trim())
			.filter((t) => t !== '');
	}

	function _parseRating(): number | null {
		const trimmed = ratingText.trim();
		if (trimmed === '') {
			return null;
		}
		const parsed = Number(trimmed);
		if (!Number.isFinite(parsed)) {
			return null;
		}
		return parsed;
	}

	async function apply(): Promise<void> {
		const add = _splitTags(tagsAdd);
		const remove = _splitTags(tagsRemove);
		if (!setRating && !setNotes && add.length === 0 && remove.length === 0) {
			error = 'set at least one field to apply';
			return;
		}
		let rating: number | undefined;
		if (setRating) {
			const parsed = _parseRating();
			if (parsed === null) {
				error = 'enter a valid rating to apply';
				return;
			}
			rating = parsed;
		}
		busy = true;
		error = null;
		try {
			const res = await bulkEditTracks({
				stable_ids: stableIds,
				expected_etags: etags,
				...(setRating ? { rating } : {}),
				...(setNotes ? { notes } : {}),
				...(add.length > 0 ? { tags_add: add } : {}),
				...(remove.length > 0 ? { tags_remove: remove } : {})
			});
			pushToast(`bulk edit applied to ${res.applied_count} track(s)`, 'info');
			onapplied();
		} catch (exc) {
			error = String(exc);
			pushToast(`bulk edit failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<EditSuiteModal title={`Bulk Edit - ${stableIds.length} track(s)`} {onclose}>
	<div class="be-form">
		<label class="be-field">
			<input type="checkbox" checked={setRating} onchange={(e) => onSetRatingToggle(e.currentTarget.checked)} />
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
				/>
			{/if}
		</label>
		<label class="be-field">
			<input type="checkbox" checked={setNotes} onchange={(e) => onSetNotesToggle(e.currentTarget.checked)} />
			Notes
			{#if notesConsensus.kind === 'mixed' && !setNotes}
				<span class="be-mixed-readout">{MIXED_READOUT}</span>
			{:else}
				<input type="text" bind:value={notes} disabled={!setNotes} placeholder="new notes text" />
			{/if}
		</label>
		<label class="be-field">
			Add tags
			<input type="text" bind:value={tagsAdd} placeholder="comma,separated,tags" />
		</label>
		<label class="be-field">
			Remove tags
			<input type="text" bind:value={tagsRemove} placeholder="comma,separated,tags" />
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
