<script lang="ts">
	/**
	 * SETNOTE-6340j: beside the pad, the songs linked in the selection - BPM and
	 * key for each, and for two the tempo gap and whether the keys mix. Kept
	 * to the first two songs so it stays a glance, not a report.
	 */
	import type { Track } from './store.svelte';
	import { compareTracks, signed } from './compare';
	import { truncateLabel } from './editor';
	import { lookupTrack } from './store.svelte';

	const COMPARE_MAX = 2;

	let { ids, left, top }: { ids: string[]; left: number; top: number } = $props();

	let rows = $state<{ id: string; track: Track | null }[]>([]);
	let error = $state<string | null>(null);

	$effect(() => {
		const want = ids.slice(0, COMPARE_MAX);
		let live = true;
		Promise.all(want.map(lookupTrack))
			.then((tracks) => {
				if (!live) return;
				rows = want.map((id, i) => ({ id, track: tracks[i] }));
				error = null;
			})
			.catch((e: unknown) => live && (error = e instanceof Error ? e.message : String(e)));
		return () => {
			live = false;
		};
	});

	const pair = $derived(
		rows.length === 2 && rows[0].track && rows[1].track ? compareTracks(rows[0].track, rows[1].track) : null
	);
	const name = (row: { id: string; track: Track | null }): string =>
		truncateLabel(row.track?.title || row.id);
</script>

<aside class="sn-compare" data-testid="set-notes-compare" style:left="{left}px" style:top="{top}px">
	{#if error}
		<p class="sn-compare-error" title={error}>Could not read songs: {error}</p>
	{/if}
	{#each rows as row (row.id)}
		<div class="sn-compare-row" data-testid="set-notes-compare-row">
			<span class="sn-compare-name" title={row.track?.title ?? `No track ${row.id} in this library`}>{name(row)}</span>
			<span
				class="readout-numeric"
				title={row.track?.bpm != null ? 'Tempo in beats per minute, from the library' : 'No BPM in the library for this song yet'}
				>{row.track?.bpm != null ? row.track.bpm.toFixed(1) : '?'} BPM</span
			>
			<span title={row.track?.key ? 'Musical key, from the library' : 'No key in the library for this song yet'}
				>{row.track?.key ?? '?'}</span
			>
		</div>
	{/each}
	{#if pair}
		<div class="sn-compare-pair" data-testid="set-notes-compare-pair">
			<span
				class="readout-numeric"
				title={pair.deltaPct === null
					? 'Needs a BPM on both songs'
					: `Second song's BPM minus the first's: ${signed(pair.deltaPct)}% of the first`}
				>{pair.deltaBpm === null ? '? BPM' : `${signed(pair.deltaBpm)} BPM`}</span
			>
			<span title="Keys mix when they are the same or one step apart on the Camelot wheel"
				>{pair.keysMix === null ? 'keys ?' : pair.keysMix ? 'keys mix' : 'keys clash'}</span
			>
		</div>
	{/if}
</aside>

<style>
	.sn-compare {
		position: fixed;
		z-index: 350;
		width: 200px;
		padding: 4px 6px;
		background: var(--sn-paper);
		color: var(--sn-ink);
		border: 1px solid var(--sn-line);
		border-radius: 4px;
		box-shadow: 0 6px 16px rgba(0, 0, 0, 0.3);
		font-size: 11px;
	}
	.sn-compare-row,
	.sn-compare-pair {
		display: grid;
		grid-template-columns: 1fr auto auto;
		gap: 6px;
		line-height: 18px;
	}
	.sn-compare-pair {
		grid-template-columns: 1fr auto;
		border-top: 1px solid var(--sn-line);
		margin-top: 2px;
		font-weight: 700;
	}
	.sn-compare-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sn-compare-error {
		margin: 0;
		color: var(--sn-err);
	}
</style>
