<script lang="ts">
	/**
	 * Learn-log console for the MIDI panel (build unit: midi panel).
	 * THE mapping-debug surface: renders the core's rolling learnLog
	 * (last LEARN_LOG_CAP = 50, newest first) via the shared MidiLearnLogRows.
	 * Mapped entries show a friendly action label; unmapped entries are
	 * highlighted red - unmapped traffic is a signal, never noise. The
	 * "pop out" button lifts the same log into a click-through floating
	 * overlay so it stays visible while driving the app.
	 */
	import { LEARN_LOG_CAP, learnLog } from '$lib/rb/midi/webmidi.svelte';
	import { openLogPopout } from '$lib/components/rb/midi/midi-ui-state.svelte';
	import MidiLearnLogRows from '$lib/components/rb/midi/MidiLearnLogRows.svelte';

	function clearLog(): void {
		learnLog.length = 0;
	}
</script>

<div class="learn-log">
	<div class="log-toolbar">
		<span class="log-count">{learnLog.length} / {LEARN_LOG_CAP}</span>
		<div class="toolbar-actions">
			<button class="log-btn" onclick={openLogPopout}>pop out</button>
			<button class="log-btn" disabled={learnLog.length === 0} onclick={clearLog}>clear</button>
		</div>
	</div>
	<MidiLearnLogRows />
</div>

<style>
	.learn-log {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-height: 0;
	}
	.log-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.log-count {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
	.toolbar-actions {
		display: flex;
		gap: 4px;
	}
	.log-btn {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 6px;
		cursor: pointer;
	}
	.log-btn:hover:not(:disabled) {
		color: var(--rb-text);
	}
	.log-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>
