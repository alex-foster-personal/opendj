<script lang="ts">
	import { ALLOWED_FIELDS, ALLOWED_OPS_BY_FIELD, FIELD_TYPES } from '$lib/smartlists/rule-schema';
	import { validateNodeOwn, type FormPredicate } from '$lib/smartlists/rule-form';

	let { predicate, onRemove }: { predicate: FormPredicate; onRemove?: () => void } = $props();

	const errors = $derived(validateNodeOwn(predicate));

	function onFieldChange(): void {
		const allowed = ALLOWED_OPS_BY_FIELD[predicate.field];
		if (!allowed.includes(predicate.op)) {
			predicate.op = allowed[0];
		}
	}

	function addListValue(): void {
		predicate.valueList.push('');
	}

	function removeListValue(index: number): void {
		predicate.valueList.splice(index, 1);
	}

	const valueInputType = $derived(FIELD_TYPES[predicate.field] === 'number' ? 'number' : 'text');
</script>

<div class="rule-predicate">
	<select bind:value={predicate.field} onchange={onFieldChange} aria-label="Field">
		{#each ALLOWED_FIELDS as field (field)}
			<option value={field}>{field}</option>
		{/each}
	</select>

	<select bind:value={predicate.op} aria-label="Operator">
		{#each ALLOWED_OPS_BY_FIELD[predicate.field] as op (op)}
			<option value={op}>{op}</option>
		{/each}
	</select>

	{#if predicate.op === 'between'}
		<input type={valueInputType} bind:value={predicate.valueLo} placeholder="lower bound" aria-label="Lower bound" />
		<span class="and-label">and</span>
		<input type={valueInputType} bind:value={predicate.valueHi} placeholder="upper bound" aria-label="Upper bound" />
	{:else if predicate.op === 'in'}
		<div class="value-list" aria-label="Values">
			{#each predicate.valueList as _value, index (index)}
				<div class="value-list-row">
					<input type="text" bind:value={predicate.valueList[index]} placeholder="value" aria-label={`Value ${index + 1}`} />
					<button type="button" onclick={() => removeListValue(index)} aria-label={`Remove value ${index + 1}`}>&times;</button>
				</div>
			{/each}
			<button type="button" onclick={addListValue}>+ value</button>
		</div>
	{:else}
		<input type={valueInputType} bind:value={predicate.value} placeholder="value" aria-label="Value" />
	{/if}

	{#if FIELD_TYPES[predicate.field] === 'date'}
		<span class="hint">ISO-8601 or relative, e.g. -7d / -6h / -2w</span>
	{/if}

	{#if onRemove}
		<button type="button" onclick={onRemove} aria-label="Remove predicate">&times;</button>
	{/if}
</div>

{#if errors.length > 0}
	<ul class="predicate-errors">
		{#each errors as error, i (i)}
			<li>{error.message}</li>
		{/each}
	</ul>
{/if}

<style>
	.rule-predicate {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
	}
	.and-label {
		color: var(--muted);
	}
	.value-list, .value-list-row {
		display: flex;
		gap: 0.4rem;
		flex-wrap: wrap;
	}
	.value-list {
		flex-direction: column;
	}
	.hint {
		color: var(--muted);
		font-size: 0.75rem;
	}
	.predicate-errors {
		margin: 0.1rem 0 0.4rem 0;
		padding-left: 1.2rem;
		color: var(--danger);
		font-size: 0.8rem;
	}
</style>
