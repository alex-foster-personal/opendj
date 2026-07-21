<script lang="ts">
	/**
	 * Learn-log console for the MIDI panel (build unit: midi panel).
	 * THE mapping-debug surface: renders the core's rolling learnLog
	 * (last LEARN_LOG_CAP = 50, newest first). Mapped entries show their
	 * dispatch note (the action type); unmapped entries are highlighted
	 * red - unmapped traffic is a signal, never noise.
	 */
	import { LEARN_LOG_CAP, learnLog } from '$lib/rb/midi/webmidi.svelte';
	import { describeSource, formatBytes, formatLogTs } from '$lib/components/rb/midi/midi-format';

	function clearLog(): void {
		learnLog.length = 0;
	}
</script>

<div class="learn-log">
	<div class="log-toolbar">
		<span class="log-count">{learnLog.length} / {LEARN_LOG_CAP}</span>
		<button class="log-clear" disabled={learnLog.length === 0} onclick={clearLog}>clear</button>
	</div>
	{#if learnLog.length === 0}
		<p class="empty">No MIDI traffic captured yet - move a control on a connected device.</p>
	{:else}
		<div class="log-rows">
			<!-- unkeyed each ON PURPOSE: performance.now() can coarsen to equal
			     ts values for burst messages, so ts is not a safe key -->
			{#each learnLog as entry}
				<div class="log-row" class:unmapped={!entry.mapped}>
					<span class="col-ts">{formatLogTs(entry.ts)}</span>
					<span class="col-dev" title={entry.deviceName}>{entry.deviceName}</span>
					<span class="col-bytes">{formatBytes(entry.status, entry.data1, entry.data2)}</span>
					<span class="col-src">{describeSource(entry.decoded)}</span>
					<span class="col-note">{entry.note}</span>
				</div>
			{/each}
		</div>
	{/if}
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
	.log-clear {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 1px 6px;
		cursor: pointer;
	}
	.log-clear:hover:not(:disabled) {
		color: var(--rb-text);
	}
	.log-clear:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.empty {
		margin: 0;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-browser);
	}
	.log-rows {
		overflow-y: auto;
		background: #060809;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
		font-size: var(--rb-fs-label);
	}
	.log-row {
		display: grid;
		grid-template-columns: 44px 90px 64px 110px minmax(0, 1fr);
		gap: 8px;
		padding: 2px 6px;
		color: var(--rb-text);
		white-space: nowrap;
	}
	.log-row:nth-child(even) {
		background: rgba(255, 255, 255, 0.02);
	}
	.log-row.unmapped {
		color: var(--rb-red);
		background: rgba(208, 52, 44, 0.12); /* --rb-red at low alpha */
	}
	.col-ts {
		color: var(--rb-text-dim);
	}
	.log-row.unmapped .col-ts {
		color: var(--rb-red);
	}
	.col-dev {
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.col-note {
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
