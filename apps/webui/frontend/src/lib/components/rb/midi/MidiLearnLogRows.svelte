<script lang="ts">
	/**
	 * Shared learn-log row list (build unit: midi panel). Renders the core's
	 * rolling learnLog (newest first) as a byte/decode/label grid. Used by
	 * both the panel drawer (MidiLearnLog) and the floating pop-out
	 * (MidiLearnLogPopout) so the friendly-label logic lives in ONE place.
	 *
	 * Decoded trace labels: mapped entries show a friendly action label
	 * ('Play (deck 1)'); unmapped entries the map DOCUMENTS show a best-guess
	 * hint ('likely: LOOP IN (deck 1)'); anything else falls back to the raw
	 * dispatch note. The raw note is always kept as the row title (hover) so
	 * no debug detail is lost. All decision logic is the tested traceLabel /
	 * bestGuessHint pair in midi-format.ts.
	 */
	import { getDeviceMap, learnLog } from '$lib/rb/midi/webmidi.svelte';
	import type { LearnLogEntry } from '$lib/rb/midi/midi-types';
	import {
		bestGuessHint,
		describeSource,
		formatBytes,
		formatLogTs,
		traceLabel
	} from '$lib/components/rb/midi/midi-format';

	/** The label a row displays: friendly action label, else documented hint,
	 * else the raw dispatch note (getDeviceMap only consulted for unmapped
	 * traffic, since a matched action already names itself). */
	function rowLabel(entry: LearnLogEntry): string {
		const hint =
			entry.action === null || entry.action === undefined
				? bestGuessHint(getDeviceMap(entry.deviceId), entry.decoded)
				: null;
		return traceLabel(entry.note, entry.action, hint);
	}
</script>

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
				<span class="col-note" title={entry.note}>{rowLabel(entry)}</span>
			</div>
		{/each}
	</div>
{/if}

<style>
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
