<!--
	CUEOUT-26: visible while the cue runs as split cue because MAIN and CUE are
	one physical output (the same device, or a MacBook's speakers and its
	occupied headphone jack). Agents read the same state as
	`headphones.split_reason` from GET /api/v1/performance/headphones.
-->
<script lang="ts">
	import { mixerState } from '$lib/rb/audio-engine.svelte';
	import { SAME_DEVICE_SPLIT_BADGE_TEXT, sameDeviceSplitTooltip } from '$lib/player/main-cue-collision';

	const label = $derived.by(() => {
		const hp = mixerState.headphones;
		if (hp.split_reason !== 'same_device' || hp.output_mode !== 'split_cable') return null;
		const id = hp.selected_master_output_device_id;
		if (id === null) return 'the system output';
		return hp.outputs.find((output) => output.id === id)?.label ?? id;
	});
</script>

{#if label !== null}
	<!-- CUEOUT-28: a compact badge; the full sentence is the hover tooltip. -->
	<div
		class="split-badge"
		role="status"
		data-testid="same-device-split-badge"
		title={sameDeviceSplitTooltip(label)}
		aria-label={sameDeviceSplitTooltip(label)}
	>
		{SAME_DEVICE_SPLIT_BADGE_TEXT}
	</div>
{/if}

<style>
	.split-badge {
		padding: 1px 5px;
		font-size: 10px;
		letter-spacing: 0.04em;
		white-space: nowrap;
		font-weight: 700;
		line-height: 1.2;
		color: var(--rb-text, #fff);
		border: 1px solid var(--rb-amber, #e0a020);
		border-radius: 3px;
	}
</style>
