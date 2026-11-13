<script lang="ts">
	/**
	 * Edge sampler on/off chip beside the waveform ZOOM control (spec 7.4, #13722),
	 * the mock's 40 x 22 `.samp-toggle`: accent border and fill when on, muted when off.
	 * Mounted by WaveformStack.svelte at the wave area's top right, so it is the keyboard
	 * selector leaf `waveforms.sampler`, live only while it renders with area.
	 */
	import { samplerToggleTitle, toggleEdgeSampler } from '$lib/player/sampler/edge-sampler-toggle';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { areaTrackedSelectorLeaf } from '../topbar-selector-node';
	import { waveSamplerSelectorId } from '../waveform-library-selector-ids';
	import './edge-sampler-tokens.css';

	const on = $derived(uiPrefs.sampler_enabled);
	const title = $derived(samplerToggleTitle(on));
</script>

<button
	type="button"
	class="sampler-toggle"
	class:sampler-on={on}
	{title}
	aria-label={title}
	aria-pressed={on}
	data-testid="edge-sampler-toggle"
	use:areaTrackedSelectorLeaf={{ id: waveSamplerSelectorId(), disabled: false }}
	onclick={toggleEdgeSampler}
>
	<svg viewBox="0 0 18 18" aria-hidden="true">
		<rect x="3.5" y="2.5" width="11" height="13" rx="1.5" />
		<path d="M1 5.5h2.5M1 9h2.5M1 12.5h2.5M14.5 5.5H17M14.5 9H17M14.5 12.5H17" />
	</svg>
</button>

<style>
	/* Mock `.samp-toggle`, off state first; `.sampler-on` is the mock's default (on) look. */
	.sampler-toggle {
		width: 40px;
		height: 22px;
		display: grid;
		place-items: center;
		padding: 0;
		border-radius: 3px;
		border: 1px solid var(--es-border-strong);
		background: var(--es-head-bg);
		color: var(--es-muted);
		box-shadow: 0 0 0 2px var(--es-toggle-ring);
		cursor: pointer;
	}
	.sampler-toggle.sampler-on {
		border-color: var(--es-accent);
		background: var(--es-ghost-bg);
		color: var(--es-accent);
	}
	.sampler-toggle svg {
		width: 16px;
		height: 16px;
		stroke: currentColor;
		fill: none;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
</style>
