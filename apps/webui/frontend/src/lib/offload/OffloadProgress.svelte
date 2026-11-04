<script lang="ts">
	/**
	 * Cloud offload progress (OFFLOAD, spec e8da14b8 section 9): one line per
	 * operation with a bar, "n of m tracks, x of y GB" and Cancel, mimicking
	 * Spotify's playlist download header. The numbers are the engine's own.
	 */
	import { isFinished, operationLabel, operationPercent } from './api-offload';
	import { offloadOps } from './offload-ops.svelte';
</script>

{#if offloadOps.ops.length > 0}
	<div class="offload-progress" data-testid="offload-progress" role="status" aria-live="polite">
		{#each offloadOps.ops as op (op.op_id)}
			{@const percent = operationPercent(op)}
			<div class="offload-op" data-op-state={op.state}>
				<span class="offload-label" title={operationLabel(op)}>{operationLabel(op)}</span>
				<span
					class="offload-bar"
					role="progressbar"
					aria-label={operationLabel(op)}
					aria-valuemin="0"
					aria-valuemax="100"
					aria-valuenow={percent ?? undefined}
				>
					<span class="offload-fill" class:indeterminate={percent === null} style={percent === null ? undefined : `width:${percent}%`}></span>
				</span>
				{#if isFinished(op)}
					<button type="button" class="offload-btn" title="Hide this line" onclick={() => offloadOps.dismiss(op.op_id)}>Dismiss</button>
				{:else}
					<button
						type="button"
						class="offload-btn"
						title="Stop after the current chunk. Nothing that is not in the cloud is deleted."
						data-testid="offload-cancel"
						onclick={() => void offloadOps.cancel(op.op_id)}>Cancel</button
					>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.offload-progress {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 4px 8px;
		border-top: 1px solid var(--rb-border, #333);
		font-size: 11px;
		color: var(--rb-text-dim, #aaa);
	}
	.offload-op {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 120px auto;
		align-items: center;
		gap: 8px;
	}
	.offload-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.offload-bar {
		position: relative;
		height: 4px;
		border-radius: 2px;
		background: var(--rb-surface-2, #2a2a2a);
		overflow: hidden;
	}
	.offload-fill {
		position: absolute;
		inset: 0 auto 0 0;
		background: var(--rb-accent, #4cc9f0);
	}
	.offload-fill.indeterminate {
		width: 30%;
		animation: offload-slide 1.2s linear infinite;
	}
	@keyframes offload-slide {
		from {
			left: -30%;
		}
		to {
			left: 100%;
		}
	}
	.offload-btn {
		font-size: 11px;
		padding: 1px 6px;
	}
</style>
