<script lang="ts">
	/**
	 * Small buildable-tier glyph, coloured by tier (green cloud = cloud,
	 * red laptop = local, amber cloud+laptop = hybrid). Inline SVG paths from
	 * types.ts (dependency-free, like the GitHub mark). The reason rides as a
	 * title tooltip; aria-label carries tier + reason for screen readers.
	 * Shared by tree rows, the fold-out detail, the header filter, and the
	 * graph legend so the icon reads identically everywhere.
	 */
	import {
		TIER_CLOUD_PATH,
		TIER_LAPTOP_PATH,
		TIER_META,
		buildableTooltip,
		type Buildable
	} from './types';

	let { buildable, size = 15 }: { buildable: Buildable; size?: number } = $props();

	const meta = $derived(TIER_META[buildable.tier]);
	const tip = $derived(buildableTooltip(buildable) ?? '');
	// Hybrid draws both glyphs, so it needs a wider box; single-glyph tiers are square.
	const w = $derived(buildable.tier === 'hybrid' ? Math.round(size * 1.35) : size);
</script>

<svg
	class="tier-icon"
	width={w}
	height={size}
	viewBox={buildable.tier === 'hybrid' ? '0 0 32 24' : '0 0 24 24'}
	role="img"
	aria-label={tip}
	fill={meta.color}
>
	<title>{tip}</title>
	{#if buildable.tier === 'cloud'}
		<path d={TIER_CLOUD_PATH} />
	{:else if buildable.tier === 'local'}
		<path d={TIER_LAPTOP_PATH} />
	{:else}
		<path d={TIER_CLOUD_PATH} transform="scale(0.8)" />
		<path d={TIER_LAPTOP_PATH} transform="translate(14 9) scale(0.6)" />
	{/if}
</svg>

<style>
	.tier-icon {
		flex: 0 0 auto;
		vertical-align: middle;
		cursor: help;
	}
</style>
