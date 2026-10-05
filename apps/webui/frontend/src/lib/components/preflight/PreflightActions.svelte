<script lang="ts">
	/**
	 * PREFLIGHT-05: the preflight screen's action buttons, their click status
	 * line, the footer note and the static explainer lines, in that order.
	 *
	 * Lifecycle-hook-free on purpose, like PreflightCheckRow: PreflightScreen
	 * owns polling (onDestroy) and so cannot be SSR-mounted under node:test,
	 * while this markup is exactly what the tests need to render.
	 *
	 * No button here carries a `title`: an explanation that only exists on
	 * hover pops in over the screen and moves what the user is reading. Each
	 * button's explanation is a static line below the footer instead
	 * (`preflightExplainers`), and each click writes `status` (see
	 * preflight-actions.ts for where that text comes from).
	 */
	import {
		ACTION_LABELS,
		type PreflightActionId,
		type PreflightExplainer
	} from '$lib/preflight/preflight-actions';

	let {
		actions,
		disabled = [],
		pending = null,
		status = null,
		footer = null,
		explainers,
		onAction = () => {}
	}: {
		actions: PreflightActionId[];
		disabled?: PreflightActionId[];
		pending?: PreflightActionId | null;
		status?: string | null;
		footer?: string | null;
		explainers: PreflightExplainer[];
		onAction?: (id: PreflightActionId) => unknown;
	} = $props();
</script>

<div class="preflight-actions">
	{#each actions as id (id)}
		<button
			type="button"
			class="preflight-action"
			class:preflight-import={id === 'import'}
			class:pending={pending === id}
			data-testid="preflight-action-{id}"
			aria-busy={pending === id}
			disabled={pending !== null || disabled.includes(id)}
			onclick={() => onAction(id)}
		>
			{id === 'import' && pending === id ? 'Opening setup...' : ACTION_LABELS[id]}
		</button>
	{/each}
</div>
<p class="preflight-action-status" data-testid="preflight-action-status" role="status" aria-live="polite">
	{status ?? ''}
</p>
{#if footer}
	<p class="note">{footer}</p>
{/if}
<ul class="note preflight-explainers" data-testid="preflight-explainers">
	{#each explainers as line (line.label)}
		<li><strong>{line.label}</strong>: {line.text}</li>
	{/each}
</ul>

<style>
	.preflight-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}
	/* Click feedback without hover feedback: the button visibly depresses on
	   press and stays dimmed while its action runs. */
	.preflight-action:active:not(:disabled) {
		transform: translateY(1px);
		filter: brightness(0.85);
	}
	.preflight-action.pending {
		filter: brightness(0.85);
		cursor: progress;
	}
	.preflight-action-status {
		margin: 0;
		min-height: 1.3em;
		font-size: 0.85em;
	}
	.note {
		color: var(--muted);
		font-size: 0.85em;
		margin: 0;
	}
	.preflight-explainers {
		list-style: none;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
</style>
