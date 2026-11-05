<!--
	BTHFP-8319: dismissable warning while Bluetooth headphones are in headset
	(call) mode, or their mic is the system input and the next app that listens
	will flip them. Agents read the same state from GET /api/v1/audio-headset-mode.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import {
		bluetoothHeadsetMode,
		dismissHeadsetModeWarning,
		headsetModeWarning,
		startBluetoothHeadsetModePolling
	} from '$lib/rb/bluetooth-headset-mode.svelte';

	const warning = $derived(headsetModeWarning(bluetoothHeadsetMode.report));

	onMount(() => startBluetoothHeadsetModePolling());
</script>

{#if warning !== null && warning.key !== bluetoothHeadsetMode.dismissedKey}
	<div
		class="bt-hfp"
		role="status"
		data-testid="bluetooth-headset-mode-banner"
		title={warning.title}
	>
		<span class="bt-hfp-copy">{warning.text}</span>
		<button
			type="button"
			class="bt-hfp-dismiss"
			aria-label="Dismiss headset mode warning"
			title="Hide until the headphones change state"
			data-testid="bluetooth-headset-mode-dismiss"
			onclick={() => dismissHeadsetModeWarning(warning.key)}
		>
			<svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
				<path
					d="M2 2 L8 8 M8 2 L2 8"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
				/>
			</svg>
		</button>
	</div>
{/if}

<style>
	.bt-hfp {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 2px 8px;
		font-size: 11px;
		font-weight: 600;
		line-height: 1.2;
		color: #1a1a1a;
		background: #f2b544;
		border-radius: 3px;
	}
	.bt-hfp-copy {
		flex: 1 1 auto;
	}
	.bt-hfp-dismiss {
		flex: 0 0 auto;
		padding: 0 4px;
		display: inline-flex;
		align-items: center;
		line-height: 1;
		color: inherit;
		background: transparent;
		border: none;
		cursor: pointer;
	}
</style>
