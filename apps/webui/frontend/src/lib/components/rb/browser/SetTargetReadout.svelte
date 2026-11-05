<script lang="ts">
	// LIBUX-8679: "~48m of 60m · 12m left" in the playlist header. Click
	// to edit the target in minutes (empty clears it). Skipped rows are left
	// out of the total; the server summary is the number shown.
	import type { SetSummaryDoc } from '$lib/rb/api-set-target';

	let {
		summary,
		onsettarget
	}: {
		summary: SetSummaryDoc | null;
		onsettarget: (targetS: number | null) => void;
	} = $props();

	let editing = $state(false);
	let draft = $state('');

	function startEdit(): void {
		draft = summary?.target_s ? String(Math.round(summary.target_s / 60)) : '';
		editing = true;
	}

	function commit(): void {
		editing = false;
		const trimmed = draft.trim();
		if (trimmed === '') return onsettarget(null);
		const minutes = Number(trimmed);
		if (Number.isFinite(minutes) && minutes > 0) onsettarget(minutes * 60);
	}

	const over = $derived((summary?.over_s ?? 0) > 0);
	const title = $derived(
		summary === null
			? ''
			: `Estimated played length of the non-skipped tracks (${summary.skipped} skipped). Click to set a target in minutes.`
	);
</script>

{#if summary !== null}
	{#if editing}
		<input
			class="set-target-input"
			data-testid="set-target-input"
			type="number"
			min="1"
			placeholder="target min"
			bind:value={draft}
			onblur={commit}
			onkeydown={(e) => {
				if (e.key === 'Enter') commit();
				else if (e.key === 'Escape') editing = false;
			}}
		/>
	{:else}
		<button class="set-target-readout" class:over data-testid="set-target-readout" {title} onclick={startEdit}>
			{summary.target_s === null ? `${summary.readout} · set target` : summary.readout}
		</button>
	{/if}
{/if}

<style>
	.set-target-readout {
		background: none;
		border: none;
		color: var(--rb-text-dim, #9aa0a6);
		font: inherit;
		font-size: 11px;
		cursor: pointer;
		white-space: nowrap;
		padding: 0 6px;
	}
	.set-target-readout.over {
		color: var(--rb-warn, #e0a050);
	}
	.set-target-input {
		width: 72px;
		font-size: 11px;
	}
</style>
