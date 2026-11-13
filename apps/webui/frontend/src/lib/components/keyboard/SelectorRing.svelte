<script lang="ts">
	/**
	 * K4 (#9396): one overlay ring like VS Code `focusBorder` (1px accent, 2px outset, no shadow).
	 * Section nodes use a dashed border. Glide vs snap default is spike KS1 (#9399); 90ms transform
	 * glide here is not the final product decision.
	 * #12253: the one polite live region for selector changes lives here, always mounted (a region
	 * inserted with its text is often not read), so no selectable control carries its own.
	 */
	import type { SelectorRingTarget } from '$lib/keyboard/selector-ring';
	import { insetSelectorRingRect, selectorRingTransform } from '$lib/keyboard/selector-ring';
	import { selectorRing } from '$lib/keyboard/selector-ring-state.svelte';

	let { target: targetProp = undefined }: { target?: SelectorRingTarget | null | undefined } =
		$props();

	const current = $derived(targetProp === undefined ? selectorRing.target : targetProp);
</script>

<div class="selector-announce" data-testid="selector-announce" role="status" aria-live="polite">
	{selectorRing.announcement}
</div>
{#if current !== null}
	{@const box = insetSelectorRingRect(current.rect)}
	<div
		class="selector-ring"
		class:section={current.kind === 'section'}
		data-testid="selector-ring"
		data-kind={current.kind}
		aria-hidden="true"
		style:transform={selectorRingTransform(box)}
		style:width={`${box.width}px`}
		style:height={`${box.height}px`}
	></div>
{/if}

<style>
	.selector-ring {
		position: absolute;
		top: 0;
		left: 0;
		box-sizing: border-box;
		border: 1px solid var(--rb-accent);
		border-radius: 2px;
		box-shadow: none;
		pointer-events: none;
		z-index: 32;
		opacity: 1;
		transition:
			transform 90ms ease-out,
			opacity 90ms ease-out;
	}
	.selector-announce {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
	.selector-ring.section {
		border-style: dashed;
	}
	@media (prefers-reduced-motion: reduce) {
		.selector-ring {
			transition: none;
		}
	}
</style>
