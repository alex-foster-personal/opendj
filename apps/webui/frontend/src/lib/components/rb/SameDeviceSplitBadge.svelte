<!--
	CUEOUT-26: visible while the cue runs as split cue because MAIN and CUE are
	one physical output (the same device, or a MacBook's speakers and its
	occupied headphone jack). Agents read the same state as
	`headphones.split_reason` from GET /api/v1/performance/headphones.
-->
<script lang="ts">
	import { mixerState } from '$lib/rb/audio-engine.svelte';
	import { sameDeviceSplitText } from '$lib/player/main-cue-collision';

	const label = $derived.by(() => {
		const hp = mixerState.headphones;
		if (hp.split_reason !== 'same_device' || hp.output_mode !== 'split_cable') return null;
		const id = hp.selected_master_output_device_id;
		if (id === null) return 'the system output';
		return hp.outputs.find((output) => output.id === id)?.label ?? id;
	});
</script>

{#if label !== null}
	<div
		class="split-badge"
		role="status"
		data-testid="same-device-split-badge"
		title="MAIN and HEADPHONE CUE are the same physical output, so the room mix plays mono on the left channel and the cue mono on the right. Pick a different MAIN output to get two separate outputs again."
	>
		{sameDeviceSplitText(label)}
	</div>
{/if}

<style>
	.split-badge {
		padding: 2px 8px;
		font-size: 11px;
		font-weight: 700;
		line-height: 1.2;
		color: var(--rb-text, #fff);
		border: 1px solid var(--rb-amber, #e0a020);
		border-radius: 3px;
	}
</style>
