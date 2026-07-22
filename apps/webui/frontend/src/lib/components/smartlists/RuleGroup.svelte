<script lang="ts">
	import { LOGICAL_OPS } from '$lib/smartlists/rule-schema';
	import { createEmptyGroup, createEmptyPredicate, validateNodeOwn, type FormGroup, type FormNode } from '$lib/smartlists/rule-form';
	import RuleGroup from './RuleGroup.svelte';
	import RulePredicate from './RulePredicate.svelte';

	let { group, onRemove }: { group: FormGroup; onRemove?: () => void } = $props();

	const errors = $derived(validateNodeOwn(group));

	function onOpChange(): void {
		if (group.op === 'not' && group.children.length > 1) {
			group.children = group.children.slice(0, 1);
		}
	}

	function addPredicate(): void {
		group.children.push(createEmptyPredicate());
	}

	function addGroup(): void {
		group.children.push(createEmptyGroup('and'));
	}

	function removeChild(index: number): void {
		group.children.splice(index, 1);
	}
</script>

<div class="rule-group">
	<div class="rule-group-header">
		<select bind:value={group.op} onchange={onOpChange} aria-label="Logical operator">
			{#each LOGICAL_OPS as op (op)}
				<option value={op}>{op.toUpperCase()}</option>
			{/each}
		</select>
		{#if onRemove}
			<button type="button" onclick={onRemove} aria-label="Remove group">&times; group</button>
		{/if}
	</div>

	{#if errors.length > 0}
		<ul class="group-errors">
			{#each errors as error, i (i)}
				<li>{error.message}</li>
			{/each}
		</ul>
	{/if}

	<div class="rule-group-children">
		{#each group.children as child, i (child.id)}
			{#if child.kind === 'group'}
				<RuleGroup group={child} onRemove={() => removeChild(i)} />
			{:else}
				<RulePredicate predicate={child} onRemove={() => removeChild(i)} />
			{/if}
		{/each}
	</div>

	{#if group.op !== 'not' || group.children.length === 0}
		<div class="rule-group-actions">
			<button type="button" onclick={addPredicate}>+ predicate</button>
			<button type="button" onclick={addGroup}>+ group</button>
		</div>
	{/if}
</div>

<style>
	.rule-group {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.6rem;
		margin: 0.3rem 0;
		background: var(--surface);
	}
	.rule-group-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.4rem;
	}
	.rule-group-children {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding-left: 0.6rem;
		border-left: 2px solid var(--border);
	}
	.rule-group-actions {
		display: flex;
		gap: 0.4rem;
		margin-top: 0.5rem;
	}
	.group-errors {
		margin: 0 0 0.4rem 0;
		padding-left: 1.2rem;
		color: var(--danger);
		font-size: 0.8rem;
	}
</style>
