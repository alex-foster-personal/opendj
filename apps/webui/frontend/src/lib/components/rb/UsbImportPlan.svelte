<script lang="ts">
	/**
	 * What Import shows on /performance: a short "likely next" bar for the
	 * set that is playing, and a windowed list of the rest of the stick.
	 */
	import VirtualList from './browser/VirtualList.svelte';
	import { closeUsbImportPlan, usbImportView } from '$lib/rb/usb-import-view.svelte';
	import type { RankedUsbImport, UsbImportPhase } from '$lib/rb/usb-import-rank';

	const ROW_HEIGHT = 28;
	const PHASE_LABEL: Record<UsbImportPhase, string> = {
		next: 'NEXT',
		soon: 'GRID',
		ready: 'READY',
		later: 'WAIT',
		skip: 'SKIP'
	};

	const overscan = $derived(usbImportView.performing ? 4 : 8);
	const counts = $derived(usbImportView.counts);
	const queue = $derived(usbImportView.queue);

	function bpmLabel(bpm: number | null): string {
		if (bpm === null || !Number.isFinite(bpm)) return '--';
		return bpm.toFixed(1);
	}

	function keyLabel(key: string | null): string {
		if (key === null || key.trim() === '') return '--';
		return key;
	}

	function meta(row: RankedUsbImport): string {
		return `${bpmLabel(row.bpm)} ${keyLabel(row.key)}`;
	}
</script>

<section
	class="plan"
	data-testid="usb-import-plan"
	data-performing={usbImportView.performing ? 'yes' : 'no'}
	aria-label="USB import analysis"
>
	<header class="plan-head">
		<strong title={usbImportView.headline}>Analyze {usbImportView.volumeName}</strong>
		<button type="button" class="hide" title="Hide the analysis plan" onclick={closeUsbImportPlan}>
			hide
		</button>
	</header>

	<p class="headline">{usbImportView.headline}</p>

	{#if usbImportView.status === 'error'}
		<p class="err" role="alert">{usbImportView.error}</p>
	{:else if usbImportView.status === 'reading'}
		<p class="dim">Reading tracks...</p>
	{:else}
		<p class="counts">
			{counts.next} likely next, {counts.soon} fit but need a grid, {counts.ready} ready, {counts.later} waiting{#if counts.skip > 0}, {counts.skip} missing{/if}
		</p>

		<p class="bar-label">Likely next</p>
		{#if usbImportView.next.length === 0}
			<p class="dim">Nothing on this stick is a likely next track.</p>
		{:else}
			<div class="bar" data-testid="usb-import-next-bar">
				{#each usbImportView.next as row (row.id)}
					<div class="chip" title={row.reason}>
						<span class="chip-title">{row.title}</span>
						<span class="chip-meta">{meta(row)}{#if row.artist} · {row.artist}{/if}</span>
						<span class="chip-why">{row.reason}</span>
					</div>
				{/each}
			</div>
		{/if}

		<p class="queue-label">
			{queue.length} tracks. Only the rows in view are mounted.
		</p>
		<div class="queue" data-testid="usb-import-queue">
			<VirtualList itemCount={queue.length} rowHeight={ROW_HEIGHT} {overscan}>
				{#snippet children(start, end)}
					{#each queue.slice(start, end) as row (row.id)}
						<div class="qrow" data-phase={row.phase} title={row.reason}>
							<span class="phase">{PHASE_LABEL[row.phase]}</span>
							<span class="title">{row.title}</span>
							<span class="meta">{meta(row)}</span>
						</div>
					{/each}
				{/snippet}
			</VirtualList>
		</div>
	{/if}
</section>

<style>
	.plan {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-height: 0;
		padding: 6px;
		border: 1px solid var(--rb-orange, #e8952a);
		background: color-mix(in srgb, var(--rb-orange, #e8952a) 8%, transparent);
	}
	.plan-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.plan-head strong {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.hide {
		flex: none;
		padding: 2px 6px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel);
		color: var(--rb-text-dim);
		cursor: pointer;
		font: inherit;
	}
	.headline,
	.counts,
	.dim,
	.err,
	.bar-label,
	.queue-label {
		margin: 0;
	}
	.headline,
	.counts,
	.queue-label {
		color: var(--rb-text-dim);
		font-size: 10px;
	}
	.dim {
		color: var(--rb-text-dim);
	}
	.err {
		color: var(--rb-red, #e05a4f);
	}
	.bar-label,
	.queue-label {
		font-weight: 700;
		color: var(--rb-text);
	}
	.bar {
		display: flex;
		gap: 6px;
		overflow-x: auto;
		padding-bottom: 2px;
	}
	.chip {
		flex: 0 0 148px;
		width: 148px;
		box-sizing: border-box;
		display: flex;
		flex-direction: column;
		gap: 1px;
		padding: 4px 6px;
		border: 1px solid var(--rb-orange, #e8952a);
		background: var(--rb-panel);
	}
	.chip-title,
	.chip-why,
	.title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chip-title {
		font-weight: 700;
	}
	.chip-meta,
	.chip-why,
	.meta,
	.phase {
		font-size: 10px;
		color: var(--rb-text-dim);
	}
	.queue {
		height: 220px;
		display: flex;
		flex-direction: column;
		min-height: 0;
		background: var(--rb-bg, #0e1218);
	}
	.qrow {
		box-sizing: border-box;
		height: 28px;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0 6px;
		white-space: nowrap;
	}
	.qrow[data-phase='next'] {
		background: color-mix(in srgb, var(--rb-orange, #e8952a) 18%, transparent);
	}
	.qrow[data-phase='soon'] {
		background: color-mix(in srgb, var(--rb-orange, #e8952a) 8%, transparent);
	}
	.phase {
		flex: none;
		width: 42px;
		font-weight: 700;
	}
	.title {
		flex: 1;
		min-width: 0;
	}
	.meta {
		flex: none;
	}
</style>
