<script lang="ts">
	/**
	 * Props-only half of CloudSyncPolicyCell: one cell of the /cloudsync policy
	 * matrix. The container derives the cell view and owns the save; this file
	 * draws the select and budget input and reports each change as a value.
	 * The element refs are bindable so the container can snap a refused or
	 * failed save back to what is stored. Labels and modes arrive as props
	 * because cloudsync-view.ts reaches the backend.
	 */
	let {
		assetKind,
		kindLabel,
		machineName,
		mode,
		unset,
		unsetOptionTitle,
		modes,
		budgetEditable,
		budgetTitle,
		budgetValue,
		onModeChange,
		onBudgetChange,
		selectEl = $bindable(),
		budgetEl = $bindable()
	}: {
		assetKind: string;
		kindLabel: string;
		machineName: string;
		/** The STORED mode, or `unset` when no row exists. */
		mode: string;
		/** The sentinel shown for an absent row. */
		unset: string;
		unsetOptionTitle: string;
		modes: readonly string[];
		budgetEditable: boolean;
		budgetTitle: string;
		budgetValue: number | '';
		onModeChange: (value: string) => void;
		onBudgetChange: (value: string) => void;
		selectEl?: HTMLSelectElement | undefined;
		budgetEl?: HTMLInputElement | undefined;
	} = $props();
</script>

<td class="policy-cell" data-testid="cloudsync-policy-cell" data-asset-kind={assetKind}>
	<select
		bind:this={selectEl}
		title={`Sync mode for ${kindLabel} on ${machineName}${mode === unset ? ' (unset: no policy stored, so this machine keeps it local-only)' : ''}`}
		value={mode}
		class:unset={mode === unset}
		onchange={(e) => onModeChange((e.currentTarget as HTMLSelectElement).value)}
	>
		<option value={unset} title={unsetOptionTitle}>{unset}</option>
		{#each modes as choice (choice)}
			<option value={choice}>{choice}</option>
		{/each}
	</select>
	<input
		bind:this={budgetEl}
		class="budget"
		type="number"
		min="0"
		step="1"
		placeholder="MB"
		disabled={!budgetEditable}
		value={budgetValue}
		title={`${budgetTitle} (${kindLabel} on ${machineName})`}
		onchange={(e) => onBudgetChange((e.currentTarget as HTMLInputElement).value)}
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
