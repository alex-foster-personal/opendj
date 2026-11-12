<script lang="ts">
	import { untrack } from 'svelte';
	import { midiState } from '$lib/rb/midi/midi-state.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		midiUi,
		noteMidiDevicesForFirstRun,
		dismissFirstRunPrompt,
		startFirstRunSetup
	} from './midi-ui-state.svelte';

	$effect(() => {
		const devices = midiState.devices;
		const dismissed = uiPrefs.midi_setup_dismissed_ports;
		untrack(() => noteMidiDevicesForFirstRun(devices, dismissed));
	});
</script>

{#if midiUi.firstRunPromptPort !== null}
	<section
		class="midi-first-run"
		role="region"
		aria-label="New MIDI controller"
		aria-live="polite"
		data-testid="midi-first-run-prompt"
	>
		<p class="midi-first-run-copy">
			<strong>{midiUi.firstRunPromptPort}</strong> connected. Open DJ has no map for it yet.
		</p>
		<div class="midi-first-run-actions">
			<button type="button" onclick={() => startFirstRunSetup()}>Set up</button>
			<button
				type="button"
				class="midi-first-run-secondary"
				onclick={() => dismissFirstRunPrompt()}
			>
				Not now
			</button>
		</div>
	</section>
{/if}

<style>
	.midi-first-run {
		position: fixed;
		right: 12px;
		bottom: 12px;
		z-index: 35;
		max-width: min(360px, calc(100vw - 24px));
		padding: 10px 12px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-accent) 12%, var(--rb-panel));
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 16px;
		box-shadow: 0 4px 12px color-mix(in srgb, var(--rb-bg) 40%, transparent);
	}

	.midi-first-run-copy {
		margin: 0 0 8px;
	}

	.midi-first-run-actions {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}

	.midi-first-run-actions button {
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 2px 10px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 18px;
		cursor: pointer;
	}

	.midi-first-run-secondary {
		color: var(--rb-text-dim);
	}
</style>
