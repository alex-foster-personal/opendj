<script lang="ts">
	// find-and-replace (issue #180): preview across the selected tracks'
	// notes field, then apply. Preview is re-fetched from the server every
	// time inputs change (never trusts a client-side guess of the current
	// value), so the diff shown is always real.
	import {
		applyFindReplace,
		previewFindReplace,
		type FindReplaceRow
	} from '$lib/rb/api-edit-suite';
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

	let find = $state('');
	let replace = $state('');
	let mode = $state<'literal' | 'regex'>('literal');
	let caseSensitive = $state(false);
	let previewRows = $state<FindReplaceRow[] | null>(null);
	let matchCount = $state(0);
	let busy = $state(false);
	let error = $state<string | null>(null);

	async function runPreview(): Promise<void> {
		if (find === '') {
			previewRows = null;
			return;
		}
		busy = true;
		error = null;
		try {
			const res = await previewFindReplace({
				field: 'notes',
				stable_ids: stableIds,
				find,
				replace,
				mode,
				case_sensitive: caseSensitive
			});
			previewRows = res.results;
			matchCount = res.match_count;
		} catch (exc) {
			error = String(exc);
			previewRows = null;
		} finally {
			busy = false;
		}
	}

	async function applyNow(): Promise<void> {
		if (previewRows === null) return;
		busy = true;
		error = null;
		try {
			await applyFindReplace({
				field: 'notes',
				stable_ids: stableIds,
				find,
				replace,
				mode,
				case_sensitive: caseSensitive,
				expected_etags: etags
			});
			pushToast(`find & replace applied to ${matchCount} track(s)`, 'info');
			onapplied();
		} catch (exc) {
			error = String(exc);
			pushToast(`find & replace failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<EditSuiteModal title={`Find & Replace - ${stableIds.length} track(s) - Notes`} {onclose}>
	<div class="fr-form">
		<label>
			Find
			<input type="text" bind:value={find} oninput={runPreview} placeholder="text to find" />
		</label>
		<label>
			Replace with
			<input type="text" bind:value={replace} oninput={runPreview} placeholder="replacement" />
		</label>
		<div class="fr-row">
			<label class="fr-inline">
				<input
					type="radio"
					name="fr-mode"
					value="literal"
					checked={mode === 'literal'}
					onchange={() => {
						mode = 'literal';
						void runPreview();
					}}
				/>
				literal
			</label>
			<label class="fr-inline">
				<input
					type="radio"
					name="fr-mode"
					value="regex"
					checked={mode === 'regex'}
					onchange={() => {
						mode = 'regex';
						void runPreview();
					}}
				/>
				regex
			</label>
			<label class="fr-inline">
				<input
					type="checkbox"
					checked={caseSensitive}
					onchange={(e) => {
						caseSensitive = e.currentTarget.checked;
						void runPreview();
					}}
				/>
				case-sensitive
			</label>
		</div>
	</div>

	{#if error !== null}
		<p class="fr-error">{error}</p>
	{/if}

	{#if previewRows !== null}
		<p class="fr-summary">{matchCount} of {stableIds.length} track(s) would change</p>
		<div class="fr-preview">
			<table>
				<thead>
					<tr>
						<th>Track</th>
						<th>Current</th>
						<th>New</th>
					</tr>
				</thead>
				<tbody>
					{#each previewRows.filter((r) => r.would_change) as row (row.stable_id)}
						<tr>
							<td>{row.stable_id}</td>
							<td>{row.current_value ?? ''}</td>
							<td>{row.new_value ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<div class="fr-actions">
		<button class="rb-lit-button" onclick={onclose} disabled={busy}>Cancel</button>
		<button
			class="rb-lit-button primary"
			onclick={applyNow}
			disabled={busy || previewRows === null || matchCount === 0}
		>
			Apply
		</button>
	</div>
</EditSuiteModal>

<style>
	.fr-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	input[type='text'] {
		background: var(--rb-bg, #14161a);
		border: 1px solid var(--rb-border, #333);
		color: var(--rb-text, #ddd);
		padding: 4px 6px;
		border-radius: 2px;
	}
	.fr-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.fr-inline {
		flex-direction: row;
		align-items: center;
		gap: 4px;
	}
	.fr-error {
		color: var(--rb-red, #e55);
		font-size: 11px;
	}
	.fr-summary {
		margin: 10px 0 4px;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	.fr-preview {
		max-height: 220px;
		overflow: auto;
		border: 1px solid var(--rb-border, #333);
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 11px;
	}
	th,
	td {
		padding: 3px 6px;
		border-bottom: 1px solid var(--rb-border, #333);
		text-align: left;
	}
	.fr-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 12px;
	}
</style>
