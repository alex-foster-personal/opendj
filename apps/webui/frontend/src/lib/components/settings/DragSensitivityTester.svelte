<script lang="ts">
	/**
	 * R6: two test knobs and two test faders for trying the drag sensitivity
	 * live. Same Knob component and the same drag code as the mixer, including
	 * shift-select groups (channels "test-a" and "test-b") and the Opt flip.
	 * Wired to nothing: values are local state, no audio and no commands.
	 */
	import Knob from '$lib/components/rb/mixer/Knob.svelte';
	import DragTestFader from './DragTestFader.svelte';
	import { dragSensitivity } from '$lib/rb/drag-sensitivity.svelte';

	interface Props {
		/** Puts the stored drag sensitivity back to its defaults. Passed in by
		 * SettingsOverlay so this tester does not import prefs itself (the
		 * prefs fan-in is ratcheted). */
		onresetsensitivity: () => void;
	}

	let { onresetsensitivity }: Props = $props();

	let knobA = $state(0.5);
	let knobB = $state(0.5);
	let faderA = $state(0.5);
	let faderB = $state(0.5);

	function resetAll(): void {
		onresetsensitivity();
		knobA = knobB = faderA = faderB = 0.5;
	}
</script>

<div class="dst" data-testid="drag-sensitivity-tester">
	<div class="dst-ch">
		<Knob knobId="test-a:knob" label="A" value={knobA} onchange={(v) => (knobA = v)} />
		<DragTestFader controlId="test-a:fader" label="Test fader A" value={faderA} onchange={(v) => (faderA = v)} />
	</div>
	<div class="dst-ch">
		<Knob knobId="test-b:knob" label="B" value={knobB} onchange={(v) => (knobB = v)} />
		<DragTestFader controlId="test-b:fader" label="Test fader B" value={faderB} onchange={(v) => (faderB = v)} />
	</div>
	<div class="dst-side">
		<span class="dst-readout">
			{dragSensitivity.base.toFixed(2)}x, half at {dragSensitivity.falloffPx} px
		</span>
		<span class="dst-help">Shift-click to group, hold Opt while dragging to flip one side</span>
		<button type="button" onclick={resetAll} title="Back to 0.70x and 150 px, and center the test controls">
			Reset drag sensitivity
		</button>
	</div>
</div>

<style>
	.dst {
		display: flex;
		align-items: center;
		gap: 16px;
		padding: 8px 4px;
	}
	.dst-ch {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.dst-side {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 11px;
	}
	.dst-help {
		opacity: 0.7;
	}
</style>
