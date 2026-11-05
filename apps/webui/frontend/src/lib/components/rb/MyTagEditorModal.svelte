<script lang="ts">
	// mytag-editor (issue #179): manage the tag catalog (derived from real
	// per-track usage, GET /mytags - no invented catalog) plus assign/
	// unassign tags across the current selection. See routes/mytag.py
	// docstring: this is the generic Track.tags list, not rekordbox's own
	// MyTag feature (ID3-level tag unification lives in apps/tags, out of
	// scope here).
	import { onMount } from 'svelte';
	import { deleteTag, listMyTags, renameTag, type MyTagSummary } from '$lib/rb/api-edit-suite';
	import { RbApiError } from '$lib/rb/api-rb-error';
	import { applyTagChange } from '$lib/rb/library-tags.svelte';
	import { runConfirmedMyTagSweep } from '$lib/rb/mytag-sweep-confirmation';
	import { pushToast } from '$lib/stores.svelte';
	import EditSuiteModal from './EditSuiteModal.svelte';

	let {
		stableIds,
		onclose,
		onapplied
	}: {
		stableIds: string[];
		onclose: () => void;
		onapplied: () => void;
	} = $props();

	let tags = $state<MyTagSummary[]>([]);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let renameTarget = $state<string | null>(null);
	let renameValue = $state('');
	let assignAdd = $state('');
	let assignRemove = $state('');

	onMount(() => void _refresh());

	async function _refresh(): Promise<void> {
		loading = true;
		error = null;
		try {
			const catalog = await listMyTags();
			tags = catalog.tags;
		} catch (exc) {
			error = String(exc);
		} finally {
			loading = false;
		}
	}

	function startRename(name: string): void {
		renameTarget = name;
		renameValue = name;
	}

	async function commitRename(): Promise<void> {
		if (renameTarget === null || renameValue.trim() === '' || renameValue === renameTarget) {
			renameTarget = null;
			return;
		}
		const sourceName = renameTarget;
		const destinationName = renameValue.trim();
		busy = true;
		try {
			if (!(await _renameOfferingMerge(sourceName, destinationName))) return;
			pushToast(`renamed "${sourceName}" -> "${destinationName}". ⌘Z undoes it.`, 'info');
			renameTarget = null;
			await _refresh();
		} catch (exc) {
			pushToast(`rename failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}

	/** R-TAG-5: the server says a name is taken, with its count; only then ask to merge. */
	async function _renameOfferingMerge(name: string, newName: string): Promise<boolean> {
		try {
			await renameTag(name, newName, false);
			return true;
		} catch (exc) {
			if (!(exc instanceof RbApiError) || exc.code !== 'CONFLICT') throw exc;
			if (!window.confirm(`${exc.message}. Merge "${name}" into it? ⌘Z undoes it.`)) return false;
			await renameTag(name, newName, true);
			return true;
		}
	}

	async function removeTagEverywhere(name: string): Promise<void> {
		const sourceTag = tags.find((tag) => tag.name === name);
		if (sourceTag === undefined) {
			error = 'catalog scope is unavailable - refresh before deleting';
			return;
		}
		busy = true;
		try {
			const result = await runConfirmedMyTagSweep(
				{ action: 'delete', tagName: name, affectedTrackCount: sourceTag.track_count },
				window.confirm,
				async () => deleteTag(name)
			);
			if (result === null) return;
			pushToast(`removed "${name}". ⌘Z undoes it.`, 'info');
			await _refresh();
		} catch (exc) {
			pushToast(`delete failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}

	function _splitTags(raw: string): string[] {
		return raw
			.split(',')
			.map((t) => t.trim())
			.filter((t) => t !== '');
	}

	async function assignToSelection(): Promise<void> {
		const add = _splitTags(assignAdd);
		const remove = _splitTags(assignRemove);
		if (add.length === 0 && remove.length === 0) {
			error = 'add or remove at least one tag';
			return;
		}
		busy = true;
		error = null;
		try {
			await applyTagChange({ stable_ids: stableIds, add, remove });
			pushToast(`MyTags updated on ${stableIds.length} track(s). ⌘Z undoes it.`, 'info');
			onapplied();
		} catch (exc) {
			error = String(exc);
			pushToast(`assign failed: ${error}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<EditSuiteModal title="MyTags" {onclose}>
	<section>
		<h3>Catalog</h3>
		{#if loading}
			<p class="mt-dim">loading...</p>
		{:else if tags.length === 0}
			<p class="mt-dim">no tags in use yet - add one via "Assign to selection" below</p>
		{:else}
			<ul class="mt-catalog">
				{#each tags as tag (tag.name)}
					<li>
						{#if renameTarget === tag.name}
							<input type="text" bind:value={renameValue} />
							<button class="rb-lit-button" onclick={commitRename} disabled={busy}>Save</button>
							<button class="rb-lit-button" onclick={() => (renameTarget = null)} disabled={busy}
								>Cancel</button
							>
						{:else}
							<span class="mt-name">{tag.name}</span>
							<span class="mt-count">{tag.track_count}</span>
							<button class="rb-lit-button" onclick={() => startRename(tag.name)} disabled={busy}
								>Rename</button
							>
							<button
								class="rb-lit-button"
								onclick={() => removeTagEverywhere(tag.name)}
								disabled={busy}
							>
								Delete
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<section>
		<h3>Assign to selection ({stableIds.length} track(s))</h3>
		<label class="mt-field">
			Add
			<input type="text" bind:value={assignAdd} placeholder="comma,separated,tags" />
		</label>
		<label class="mt-field">
			Remove
			<input type="text" bind:value={assignRemove} placeholder="comma,separated,tags" />
		</label>
		{#if error !== null}
			<p class="mt-error">{error}</p>
		{/if}
		<div class="mt-actions">
			<button
				class="rb-lit-button primary"
				onclick={assignToSelection}
				disabled={busy || stableIds.length === 0}
				title={stableIds.length === 0 ? 'select one or more tracks first' : ''}
			>
				Apply to selection
			</button>
		</div>
	</section>
</EditSuiteModal>

<style>
	section {
		margin-bottom: 14px;
	}
	h3 {
		margin: 0 0 6px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim);
	}
	.mt-dim {
		color: var(--rb-text-dim);
		font-size: 11px;
	}
	.mt-catalog {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.mt-catalog li {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 11px;
	}
	.mt-name {
		flex: 1;
	}
	.mt-count {
		color: var(--rb-text-dim);
		min-width: 20px;
		text-align: right;
	}
	.mt-field {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 11px;
		color: var(--rb-text-dim);
		margin-bottom: 8px;
	}
	.mt-field input {
		background: var(--rb-bg, #14161a);
		border: 1px solid var(--rb-border, #333);
		color: var(--rb-text, #ddd);
		padding: 4px 6px;
		border-radius: 2px;
	}
	.mt-error {
		color: var(--rb-red, #e55);
		font-size: 11px;
	}
	.mt-actions {
		display: flex;
		justify-content: flex-end;
	}
</style>
