<script lang="ts">
	import { plannedTitle } from '$lib/rb/planned-explainers';

	/** Interactive comment-pin controls in FeedbackWidget's explainer. */
	let {
		pinsVisible,
		showAgentPins,
		ontoggle,
		onToggleAgentPins
	}: {
		pinsVisible: boolean;
		showAgentPins: boolean;
		ontoggle: () => void;
		onToggleAgentPins: () => void;
	} = $props();
</script>

<label class="fb-toggle-row">
	<input type="checkbox" checked={pinsVisible} onchange={ontoggle} />
	Show feedback comment pins
</label>
<label class="fb-toggle-row">
	<input type="checkbox" checked={showAgentPins} onchange={onToggleAgentPins} />
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
