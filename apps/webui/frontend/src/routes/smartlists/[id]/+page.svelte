<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getSmartlistTracks, listSmartlists, type SmartlistOut, type SmartlistTrackOut } from '$lib/api';
	import {
		astToForm,
		createEmptyGroup,
		formToAst,
		isFormGroup,
		validateForm,
		type FormNode
	} from '$lib/smartlists/rule-form';
	import { ORDER_BY_OPTIONS } from '$lib/smartlists/rule-schema';
	import { pushToast } from '$lib/stores.svelte';
	import RuleGroup from '$lib/components/smartlists/RuleGroup.svelte';
	import RulePredicate from '$lib/components/smartlists/RulePredicate.svelte';

	let smartlist = $state<SmartlistOut | null>(null);
	let notFound = $state(false);
	let loading = $state(true);
	let formRoot = $state<FormNode | null>(null);
	let orderBy = $state('added_date desc');
	let tracks = $state<SmartlistTrackOut[]>([]);
	let tracksError = $state<string | null>(null);

	const errors = $derived(formRoot ? validateForm(formRoot) : []);
	const astResult = $derived.by(() => {
		if (!formRoot) return null;
		try {
			return { ok: true as const, ast: formToAst(formRoot) };
		} catch (exc) {
			return { ok: false as const, error: (exc as Error).message };
		}
	});

	function wrapInGroup(): void {
		if (!formRoot || isFormGroup(formRoot)) return;
		const group = createEmptyGroup('and');
		group.children = [formRoot];
		formRoot = group;
	}

	onMount(async () => {
		const id = $page.params.id;
		if (id === undefined) throw new Error('smartlists route param "id" missing');
		try {
			const all = await listSmartlists();
			const found = all.find((s) => s.id === id);
			if (!found) {
				notFound = true;
				return;
			}
			smartlist = found;
			orderBy = found.order_by;
			formRoot = astToForm(found.rule);
		} catch (exc) {
			pushToast(`Failed to load smartlist: ${exc}`, 'error');
		} finally {
			loading = false;
		}

		try {
			tracks = await getSmartlistTracks(id);
		} catch (exc) {
			tracksError = `${exc}`;
		}
	});
</script>

<a href="/smartlists">&larr; back</a>

{#if loading}
	<p>Loading...</p>
{:else if notFound}
	<p style="color: var(--danger);">Smartlist not found.</p>
{:else if smartlist && formRoot}
	<h2>{smartlist.name}</h2>

	<div class="order-by-row">
		<label>
			Order by
			<select bind:value={orderBy}>
				{#each ORDER_BY_OPTIONS as option (option)}
					<option value={option}>{option}</option>
				{/each}
			</select>
		</label>
	</div>

	<h3>Rule</h3>
	{#if isFormGroup(formRoot)}
		<RuleGroup group={formRoot} />
	{:else}
		<RulePredicate predicate={formRoot} />
		<button type="button" onclick={wrapInGroup}>Wrap in group to add more conditions</button>
	{/if}

	<div class="validation-summary">
		{#if errors.length > 0}
			<p style="color: var(--danger);">{errors.length} validation {errors.length === 1 ? 'error' : 'errors'}:</p>
			<ul>
				{#each errors as error, i (i)}
					<li>{error.path}: {error.message}</li>
				{/each}
			</ul>
		{:else}
			<p style="color: var(--muted);">Rule is valid.</p>
		{/if}
	</div>

	<h3>AST preview</h3>
	{#if astResult?.ok}
		<pre class="ast-preview">{JSON.stringify({ rule: astResult.ast, order_by: orderBy }, null, 2)}</pre>
	{:else}
		<p style="color: var(--danger);">{astResult?.error}</p>
	{/if}

	<div class="save-row">
		<button class="primary" disabled title="write path pending">Save</button>
	</div>

	<h3>Currently matching tracks</h3>
	{#if tracksError}
		<p style="color: var(--muted);">Preview unavailable: {tracksError}</p>
	{:else if tracks.length === 0}
		<p style="color: var(--muted);">No tracks currently match this smartlist's saved rule.</p>
	{:else}
		<table class="library">
			<thead>
				<tr><th>Title</th><th>Artist</th><th>BPM</th><th>Key</th><th>Rating</th><th>Genre</th></tr>
			</thead>
			<tbody>
				{#each tracks as t (t.stable_id)}
					<tr>
						<td><a href={`/track/${t.stable_id}`}>{t.title ?? t.stable_id}</a></td>
						<td>{t.artist ?? ''}</td>
						<td>{t.bpm ?? ''}</td>
						<td>{t.key ?? ''}</td>
						<td>{t.rating ?? ''}</td>
						<td>{t.genre ?? ''}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
{/if}

<style>
	.order-by-row {
		margin: 0.5rem 0 1rem 0;
	}
	.ast-preview {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.8rem;
		overflow-x: auto;
		font-size: 0.85rem;
	}
	.save-row {
		margin: 1rem 0;
	}
	.validation-summary {
		margin: 0.5rem 0;
	}
</style>
