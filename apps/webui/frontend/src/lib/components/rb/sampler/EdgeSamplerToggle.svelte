<script lang="ts">
	/** Edge sampler on/off beside waveform ZOOM (spec 7.4, EDGESAMP-21). */
	import { setSamplerEnabledPref } from '$lib/rb/prefs.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	const on = $derived(uiPrefs.sampler_enabled);
	const title = $derived(
		on
			? 'Edge sampler ON: edges open on hover. Click to turn off.'
			: 'Edge sampler OFF: edges stay closed; playing tabs stay out. Click to turn on.'
	);

	function toggle(): void {
		setSamplerEnabledPref(!on);
	}
</script>

<button
	type="button"
	class="tb-icon theme-toggle topbar-slot-pinned sampler-toggle"
	class:sampler-on={on}
	{title}
	aria-label={title}
	aria-pressed={on}
	data-testid="edge-sampler-toggle"
	onclick={toggle}
>
	<svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true">
		<rect x="3.5" y="2.5" width="11" height="13" rx="1.5" />
		<path d="M1 5.5h2.5M1 9h2.5M1 12.5h2.5M14.5 5.5H17M14.5 9H17M14.5 12.5H17" />
	</svg>
</button>

<style>
	.sampler-toggle {
		padding: 2px 4px;
		color: var(--rb-text-dim);
	}
	.sampler-toggle.sampler-on {
		color: var(--rb-orange);
	}
	.sampler-toggle svg {
		stroke: currentColor;
		fill: none;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
</style>
