<script lang="ts">
	/**
	 * The single floating hover explainer for the KPI panel. Anchors itself to
	 * whatever element is on top of the tip stack, flipping below the target
	 * when there is no room above and clamping to the viewport.
	 */
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { tipState } from './tooltip.svelte';

	let el = $state<HTMLDivElement | null>(null);
	let x = $state(0);
	let y = $state(0);

	const entry = $derived(tipState.entry);

	$effect(() => {
		const active = entry;
		if (!active || !el) return;
		const anchor = active.node.getBoundingClientRect();
		const self = el.getBoundingClientRect();
		let proposedTop = anchor.top - self.height - 8;
		if (proposedTop < 8) proposedTop = anchor.bottom + 8;
		const box = clampToViewport(
			anchor.left + anchor.width / 2 - self.width / 2,
			proposedTop,
			{ width: self.width, height: self.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		x = box.x;
		y = box.y;
	});
</script>

{#if entry}
	<div class="tip" bind:this={el} style="left: {x}px; top: {y}px;" role="tooltip">
		<strong>{entry.content.title}</strong>
		{#if entry.content.subtitle}
			<div class="sub">{entry.content.subtitle}</div>
		{/if}
		{#each entry.content.lines ?? [] as line}
			<div class="line {line.tone ?? ''}">{line.text}</div>
		{/each}
		{#each entry.content.body ?? [] as para}
			<p>{para}</p>
		{/each}
	</div>
{/if}

<style>
	.tip {
		position: fixed;
		z-index: 60;
		max-width: 300px;
		background: var(--surface-raised);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.5rem 0.65rem;
		font-size: 0.75rem;
		line-height: 1.45;
		color: var(--fg);
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
		pointer-events: none;
	}
	.sub {
		color: var(--muted);
	}
	.line {
		margin-top: 0.2rem;
		font-variant-numeric: tabular-nums;
	}
	.line.good {
		color: var(--kpi-ok);
	}
	.line.bad {
		color: var(--danger);
	}
	.line.flat {
		color: var(--muted);
	}
	.tip p {
		margin: 0.45rem 0 0 0;
	}
</style>
