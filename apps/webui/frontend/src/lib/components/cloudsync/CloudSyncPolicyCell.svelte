<script lang="ts">
	/**
	 * One cell of the /cloudsync policy matrix. An absent row reads 'unset'
	 * and is never shown or saved as 'stream' (the backend refuses a default).
	 * 'unset' is offered on every cell: picking it on a stored cell clears the
	 * row (DELETE /policies/{machine_id}/{asset_kind}), and an unset cell means
	 * this machine keeps that kind local-only (CLOUDSYNC-33).
	 * The budget input is live only while the STORED mode is cached.
	 */
	import { policyErrorMessage, SYNC_MODES, type AssetKind, type SyncPolicy } from '$lib/api-cloudsync';

	import {
		UNSET,
		UNSET_OPTION_TITLE,
		commitPolicyDecision,
		policyBudgetChange,
		policyCellView,
		policyClearedNote,
		policyModeChange,
		type PolicyModeDecision
	} from './cloudsync-view';

	interface Props {
		machineId: string;
		machineName: string;
		assetKind: AssetKind;
		kindLabel: string;
		policy: SyncPolicy | undefined;
		onSaved: (saved: SyncPolicy) => void;
		/** The stored row was cleared back to unset; `note` is info, never an error. */
		onCleared: (machineId: string, assetKind: AssetKind, note: string) => void;
		/** A refusal or failed save, for the page to surface (its toast). */
		onError: (message: string) => void;
	}

	let { machineId, machineName, assetKind, kindLabel, policy, onSaved, onCleared, onError }: Props =
		$props();

	const view = $derived(policyCellView(policy));
	let selectEl: HTMLSelectElement | undefined = $state();
	let budgetEl: HTMLInputElement | undefined = $state();

	/** Show what is STORED again: a refusal or a failed save wrote nothing. */
	function snapBackToStored(): void {
		if (selectEl) selectEl.value = view.mode;
		if (budgetEl) budgetEl.value = policy?.cache_budget_mb?.toString() ?? '';
	}

	async function apply(decision: PolicyModeDecision, what: string): Promise<void> {
		if (decision.kind === 'noop') return;
		if (decision.kind === 'refuse') {
			onError(decision.reason);
			snapBackToStored();
			return;
		}
		try {
			const result = await commitPolicyDecision(decision);
			if (result.kind === 'saved') {
				onSaved(result.policy);
			} else {
				onCleared(result.machineId, result.assetKind, policyClearedNote(kindLabel, machineName, result.outcome));
			}
		} catch (exc) {
			const verb = decision.kind === 'delete' ? 'clear' : 'save';
			onError(`Failed to ${verb} ${what}: ${policyErrorMessage(exc)}`);
			snapBackToStored();
		}
	}
</script>

<td class="policy-cell" data-testid="cloudsync-policy-cell" data-asset-kind={assetKind}>
	<select
		bind:this={selectEl}
		title={`Sync mode for ${kindLabel} on ${machineName}${view.mode === UNSET ? ' (unset: no policy stored, so this machine keeps it local-only)' : ''}`}
		value={view.mode}
		class:unset={view.mode === UNSET}
		onchange={(e) =>
			apply(
				policyModeChange(machineId, assetKind, policy, (e.currentTarget as HTMLSelectElement).value),
				'policy'
			)}
	>
		<option value={UNSET} title={UNSET_OPTION_TITLE}>{UNSET}</option>
		{#each SYNC_MODES as mode (mode)}
			<option value={mode}>{mode}</option>
		{/each}
	</select>
	<input
		bind:this={budgetEl}
		class="budget"
		type="number"
		min="0"
		step="1"
		placeholder="MB"
		disabled={!view.budgetEditable}
		value={policy?.cache_budget_mb ?? ''}
		title={`${view.budgetTitle} (${kindLabel} on ${machineName})`}
		onchange={(e) =>
			apply(
				policyBudgetChange(machineId, assetKind, policy, (e.currentTarget as HTMLInputElement).value),
				'cache budget'
			)}
	/>
</td>

<style>
	td.policy-cell {
		display: flex;
		align-items: center;
		gap: 6px;
		white-space: nowrap;
	}
	select,
	input.budget {
		background: var(--bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 3px 6px;
		font-size: 0.82rem;
	}
	select.unset {
		color: var(--muted);
		font-style: italic;
	}
	input.budget {
		width: 5.5em;
	}
	input.budget:disabled {
		opacity: 0.4;
	}
</style>
