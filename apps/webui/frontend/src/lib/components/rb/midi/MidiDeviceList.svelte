<script lang="ts">
	/**
	 * Connected-devices list for the MIDI panel (build unit: midi panel).
	 * Real state only: rows come from midiState.devices; map/binding info
	 * from getDeviceMap(). No placeholder rows ever.
	 *
	 * LED test: flashes every LedRule the device's map declares (velocityOn,
	 * then velocityOff after LED_TEST_FLASH_MS). No Note/CC numbers live in
	 * this file - the flashed (ch, note, velocity) values come exclusively
	 * from the registered DeviceMap, which carries its own per-mapping
	 * source citations (midi-types.ts contract).
	 */
	import { getDeviceMap, midiState, sendLed } from '$lib/rb/midi/webmidi.svelte';
	import type { MidiDeviceInfo } from '$lib/rb/midi/webmidi.svelte';
	import type { LedRule } from '$lib/rb/midi/midi-types';

	const LED_TEST_FLASH_MS = 600;

	/** Device ids with a flash currently in flight (disables the button). */
	let flashing = $state<string[]>([]);

	function _ledRules(deviceId: string): LedRule[] {
		return getDeviceMap(deviceId)?.leds ?? [];
	}

	function _bindingCount(deviceId: string): number {
		return getDeviceMap(deviceId)?.bindings.length ?? 0;
	}

	function _ledTestDisabledReason(device: MidiDeviceInfo): string | null {
		if (device.mapVendor === null) return 'no device map matched - nothing to flash';
		if (!device.hasOutput) return 'device has no MIDI output port';
		if (_ledRules(device.id).length === 0) return 'device map declares no LedRules';
		if (flashing.includes(device.id)) return 'flash in progress';
		return null;
	}

	function testLeds(device: MidiDeviceInfo): void {
		const reason = _ledTestDisabledReason(device);
		if (reason !== null) {
			throw new Error(`LED test on ${device.name}: ${reason}`);
		}
		const rules = _ledRules(device.id);
		flashing = [...flashing, device.id];
		for (const rule of rules) {
			sendLed(device.id, rule.out.ch, rule.out.note, rule.out.velocityOn);
		}
		setTimeout(() => {
			// Restore to the OFF velocity; the glue's reactive LED sync re-lights
			// anything whose trigger is genuinely active on the next state change.
			for (const rule of rules) {
				sendLed(device.id, rule.out.ch, rule.out.note, rule.out.velocityOff);
			}
			flashing = flashing.filter((id) => id !== device.id);
		}, LED_TEST_FLASH_MS);
	}
</script>

<div class="device-list">
	{#if midiState.devices.length === 0}
		<p class="empty">
			{midiState.permission === 'granted'
				? 'No MIDI devices connected - plug in a controller (hot-plug is live).'
				: 'Devices appear here once MIDI access is granted.'}
		</p>
	{/if}
	{#each midiState.devices as device (device.id)}
		<div class="device-row">
			<div class="device-id">
				<span class="device-name">{device.name}</span>
				{#if device.manufacturer !== ''}
					<span class="device-mfr">{device.manufacturer}</span>
				{/if}
			</div>
			<div class="device-meta">
				{#if device.mapVendor !== null}
					<span class="chip chip-mapped">{device.mapVendor}</span>
					<span class="chip">{_bindingCount(device.id)} bindings</span>
				{:else}
					<span class="chip chip-unmapped">no map - learn log only</span>
				{/if}
				<span class="chip" class:chip-dim={!device.hasOutput}>
					{device.hasOutput ? 'LED out' : 'no output'}
				</span>
				<button
					class="led-test"
					disabled={_ledTestDisabledReason(device) !== null}
					title={_ledTestDisabledReason(device) ?? `flash all ${_ledRules(device.id).length} mapped LEDs`}
					onclick={() => testLeds(device)}
				>
					LED test
				</button>
			</div>
		</div>
	{/each}
</div>

<style>
	.device-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.empty {
		margin: 0;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-browser);
	}
	.device-row {
		display: flex;
		flex-direction: column;
		gap: 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 6px 8px;
	}
	.device-id {
		display: flex;
		align-items: baseline;
		gap: 8px;
	}
	.device-name {
		color: var(--rb-text);
		font-size: var(--rb-fs-browser);
		font-weight: 600;
	}
	.device-mfr {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
	.device-meta {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.chip {
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 1px 5px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text);
		line-height: 1.4;
	}
	.chip-mapped {
		color: var(--rb-green);
		border-color: var(--rb-green);
	}
	.chip-unmapped {
		color: var(--rb-red);
		border-color: var(--rb-red);
	}
	.chip-dim {
		color: var(--rb-text-dim);
	}
	.led-test {
		margin-left: auto;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 2px 8px;
		cursor: pointer;
	}
	.led-test:hover:not(:disabled) {
		border-color: var(--rb-accent);
		color: #fff;
	}
	.led-test:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>
