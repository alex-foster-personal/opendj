<script lang="ts">
	/**
	 * One cell of the /cloudsync policy matrix. An absent row reads 'unset'
	 * and is never shown or saved as 'stream' (the backend refuses a default).
	 * The budget input is live only while the STORED mode is cached.
	 */
	import { putPolicy, SYNC_MODES, type AssetKind, type SyncPolicy } from '$lib/api-cloudsync';

	import {
		INERT_TOOLTIP,
		UNSET,
		policyBudgetChange,
		policyCellView,
		policyModeChange,
		type PolicyPutDecision
	} from './cloudsync-view';

	interface Props {
		machineId: string;
		machineName: string;
		assetKind: AssetKind;
		kindLabel: string;
		policy: SyncPolicy | undefined;
		onSaved: (saved: SyncPolicy) => void;
		/** A refusal or failed save, for the page to surface (its toast). */
		onError: (message: string) => void;
	}

	let { machineId, machineName, assetKind, kindLabel, policy, onSaved, onError }: Props = $props();

	const view = $derived(policyCellView(policy));
	let selectEl: HTMLSelectElement | undefined = $state();
	let budgetEl: HTMLInputElement | undefined = $state();

	/** Show what is STORED again: a refusal or a failed save wrote nothing. */
	function snapBackToStored(): void {
		if (selectEl) selectEl.value = view.mode;
		if (budgetEl) budgetEl.value = policy?.cache_budget_mb?.toString() ?? '';
	}

	async function apply(decision: PolicyPutDecision, what: string): Promise<void> {
		if (decision.kind === 'refuse') {
			onError(decision.reason);
			snapBackToStored();
			return;
		}
		try {
			onSaved(await putPolicy(decision.body));
		} catch (exc) {
			onError(`Failed to save ${what}: ${exc instanceof Error ? exc.message : String(exc)}`);
			snapBackToStored();
		}
	}
</script>

<td class="policy-cell" data-testid="cloudsync-policy-cell" data-asset-kind={assetKind}>
	<select
		bind:this={selectEl}
		title={`Sync mode for ${kindLabel} on ${machineName}${view.mode === UNSET ? ' (no policy stored; the backend refuses to default one)' : ''}`}
		value={view.mode}
		class:unset={view.mode === UNSET}
		onchange={(e) =>
			apply(
				policyModeChange(machineId, assetKind, policy, (e.currentTarget as HTMLSelectElement).value),
				'policy'
			)}
	>
		{#if view.mode === UNSET}
			<option
				value={UNSET}
				title={`No policy is stored for this cell. Picking a mode stores one; clearing a stored policy back to unset is ${INERT_TOOLTIP}`}
				>{UNSET}</option
			>
		{/if}
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
