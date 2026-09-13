<script lang="ts">
	// bulk-edit (issue #178): set rating / notes / tags across every
	// selected track in one atomic request (backend pre-checks every row's
	// ETag before writing any of them - see routes/bulk_edit.py).
	import { bulkEditTracks } from '$lib/rb/api-edit-suite';
	import { pushToast } from '$lib/stores.svelte';
	import EditSuiteModal from './EditSuiteModal.svelte';

	let {
		stableIds,
		etags,
		onclose,
		onapplied
	}: {
		stableIds: string[];
		etags: Record<string, string>;
		onclose: () => void;
		onapplied: () => void;
	} = $props();

	let setRating = $state(false);
	let rating = $state(3);
	let setNotes = $state(false);
	let notes = $state('');
	let setGenre = $state(false);
	let genre = $state('');
	let setComments = $state(false);
	let comments = $state('');
	let tagsAdd = $state('');
	let tagsRemove = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	function _splitTags(raw: string): string[] {
		return raw
			.split(',')
			.map((t) => t.trim())
			.filter((t) => t !== '');
	}

	async function apply(): Promise<void> {
		const add = _splitTags(tagsAdd);
		const remove = _splitTags(tagsRemove);
		if (
			!setRating &&
			!setNotes &&
			!setGenre &&
			!setComments &&
			add.length === 0 &&
			remove.length === 0
		) {
			error = 'set at least one field to apply';
			return;
		}
		busy = true;
		error = null;
		try {
			const res = await bulkEditTracks({
				stable_ids: stableIds,
				expected_etags: etags,
				...(setRating ? { rating } : {}),
				...(setNotes ? { notes } : {}),
				...(setGenre ? { genre } : {}),
				...(setComments ? { comments } : {}),
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
			<input type="checkbox" bind:checked={setRating} />
			Rating
			<input type="number" min="0" max="5" bind:value={rating} disabled={!setRating} />
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setNotes} />
			Notes
			<input type="text" bind:value={notes} disabled={!setNotes} placeholder="new notes text" />
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setGenre} />
			Genre
			<input type="text" bind:value={genre} disabled={!setGenre} placeholder="new genre text" />
		</label>
		<label class="be-field">
			<input type="checkbox" bind:checked={setComments} />
			Comments
			<input
				type="text"
				bind:value={comments}
				disabled={!setComments}
				placeholder="new comments text"
			/>
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
