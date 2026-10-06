<!--
	CUEOUT-25: persistent, loud banner while MAIN plays into the HEADPHONE CUE
	device (the room hears nothing) or the MAIN route failed. No timeout and no
	dismiss: it clears only when the fault does. [Fix] pins MAIN to a room
	output; agents do the same with POST /api/v1/performance/headphones/outputs/master.
-->
<script lang="ts">
	import { engine, mixerState } from '$lib/rb/audio-engine.svelte';
	import { preferredMasterOutputDeviceId } from '$lib/player/headphones';
	import { mainOutputFault, mainOutputFaultText } from '$lib/player/main-cue-collision';

	let busy = $state(false);
	let fixError = $state<string | null>(null);

	const fault = $derived(
		mainOutputFault(
			mixerState.headphones,
			preferredMasterOutputDeviceId(mixerState.headphones.outputs, mixerState.headphones.selected_output_device_id)
		)
	);
	const fixLabel = $derived(
		fault?.fixDeviceId == null
			? null
			: (mixerState.headphones.outputs.find((output) => output.id === fault.fixDeviceId)?.label ?? fault.fixDeviceId)
	);

	async function fixMainOutput(): Promise<void> {
		if (busy || fault === null || fault.fixDeviceId === null) return;
		busy = true;
		fixError = null;
		try {
			await engine.selectMasterOutput(fault.fixDeviceId);
		} catch (error) {
			fixError = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}
</script>

{#if fault !== null}
	<div class="main-fault" role="alert" data-testid="main-output-fault-banner" data-fault-kind={fault.kind}>
		<span class="main-fault-copy">{mainOutputFaultText(fault)}</span>
		<button
			type="button"
			class="main-fault-fix"
			disabled={busy || fault.fixDeviceId === null}
			title={fixLabel === null
				? 'No other output device is connected: plug in speakers or unplug the headphones'
				: `Send MAIN to ${fixLabel}`}
			onclick={() => void fixMainOutput()}
		>
			{busy ? 'Fixing...' : 'Fix'}
		</button>
		{#if fixError !== null}
			<span class="main-fault-error" title="Why the Fix did not apply">{fixError}</span>
		{/if}
	</div>
{/if}

<style>
	.main-fault {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 2px 8px;
		font-size: 11px;
		font-weight: 700;
		line-height: 1.2;
		color: #fff;
		background: var(--rb-red, #c33);
		border-radius: 3px;
	}
	.main-fault-fix {
		flex: 0 0 auto;
		font-size: 11px;
		font-weight: 700;
		padding: 1px 8px;
		border: 1px solid currentColor;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.main-fault-fix:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.main-fault-error {
		font-weight: 400;
	}
</style>
