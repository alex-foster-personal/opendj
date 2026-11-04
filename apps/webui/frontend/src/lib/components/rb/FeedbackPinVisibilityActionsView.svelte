<script lang="ts">
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { PIN_VISIBLE_DAYS_MAX, PIN_VISIBLE_DAYS_MIN } from '$lib/rb/feedback-pin-age';

	/** Props-only comment-pin toggles. FeedbackPinVisibilityActions.svelte reads
	 * and writes the agent-pins pref and passes it in. The third row is inert
	 * until its data source exists. */
	let {
		pinsVisible,
		showAgentPins,
		ontoggle,
		ontoggleagentpins,
		pinVisibleDays,
		onvisibledays
	}: {
		pinsVisible: boolean;
		showAgentPins: boolean;
		ontoggle: () => void;
		ontoggleagentpins: () => void;
		/** How many days a pin stays visible; 0 shows every pin. */
		pinVisibleDays: number;
		/** The typed value, raw; the container validates and stores it. */
		onvisibledays: (raw: string) => void;
	} = $props();
</script>

<label class="fb-toggle-row">
	<input type="checkbox" checked={pinsVisible} onchange={ontoggle} />
	Show feedback comment pins
</label>
<label class="fb-toggle-row">
	<input type="checkbox" checked={showAgentPins} onchange={ontoggleagentpins} />
	Show agent pins
</label>
<label class="fb-toggle-row rb-inert" title={plannedTitle('feedback-pin-visibility')}>
	<input type="checkbox" disabled />
	Show other users' pins
	<span class="fb-hint">(not implemented - community feature)</span>
</label>
<label class="fb-days">
	<span>How many days should pins be visible?</span>
	<input
		type="number"
		min={PIN_VISIBLE_DAYS_MIN}
		max={PIN_VISIBLE_DAYS_MAX}
		step="1"
		value={pinVisibleDays}
		oninput={(event) => onvisibledays(event.currentTarget.value)}
	/>
	<span class="fb-days-hint">0 shows every pin. Comments older than this are hidden.</span>
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
	.fb-days {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
		margin-top: 8px;
		cursor: pointer;
	}
	.fb-days input {
		width: 72px;
		margin: 0;
		padding: 2px 4px;
		background: var(--rb-panel-raised, #12141a);
		color: var(--rb-text, #eee);
		border: 1px solid var(--rb-border, #333);
		border-radius: 2px;
		font: inherit;
	}
	.fb-days-hint {
		color: var(--rb-text-dim, #999);
	}
</style>
