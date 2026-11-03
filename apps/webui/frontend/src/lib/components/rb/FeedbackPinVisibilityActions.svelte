<script lang="ts">
	import { setShowAgentPins, uiPrefs } from '$lib/rb/prefs.svelte';
	import FeedbackPinVisibilityActionsView from './FeedbackPinVisibilityActionsView.svelte';

	/** Interactive comment-pin controls in the FeedbackWidget and shell-button
	 * explainers. The agent-pins switch reads and writes the HTTP ui-prefs
	 * preference here, once, so neither host imports prefs for it (prefs
	 * fan-in ratchet). The rendering lives in the props-only view. */
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

<FeedbackPinVisibilityActionsView
	{pinsVisible}
	showAgentPins={uiPrefs.show_agent_pins}
	{ontoggle}
	ontoggleagentpins={toggleAgentPins}
/>
