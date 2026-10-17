<script lang="ts">
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { setShowAgentPins, uiPrefs } from '$lib/rb/prefs.svelte';

	/** Interactive comment-pin controls in the FeedbackWidget and shell-button
	 * explainers. The agent-pins switch reads and writes the HTTP ui-prefs
	 * preference here, once, so neither host imports prefs for it (prefs
	 * fan-in ratchet). */
	let {
		pinsVisible,
		ontoggle
	}: {
		pinsVisible: boolean;
		ontoggle: () => void;
	} = $props();

	function toggleAgentPins(): void {
		setShowAgentPins(!uiPrefs.show_agent_pins);
	}
</script>

<label class="fb-toggle-row">
	<input type="checkbox" checked={pinsVisible} onchange={ontoggle} />
	Show feedback comment pins
</label>
<label class="fb-toggle-row">
	<input type="checkbox" checked={uiPrefs.show_agent_pins} onchange={toggleAgentPins} />
	Show agent pins
</label>
<label class="fb-toggle-row rb-inert" title={plannedTitle('feedback-pin-visibility')}>
	<input type="checkbox" disabled />
	Show other users' pins
	<span class="fb-hint">(not implemented - community feature)</span>
</label>

<style>
	.fb-toggle-row {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: 4px;
		cursor: pointer;
	}
	.fb-toggle-row.rb-inert {
		cursor: default;
	}
	.fb-toggle-row input[type='checkbox'] {
		margin: 0;
	}
</style>
