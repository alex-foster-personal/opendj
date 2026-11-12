<script lang="ts">
	import type { TracklistRow } from './sets-api';
	import type { TracklistCopyFormat, TracklistFileFormat } from './sets-api';
	import { COPY_FORMATS, DOWNLOAD_FORMATS, EMPTY_HINT, HIDDEN_HINT } from './set-tracklist';

	interface Props {
		sessionId: string;
		rows: TracklistRow[];
		soundcloudReady: boolean;
		oncopy: (format: TracklistCopyFormat) => void;
		ondownload: (format: TracklistFileFormat) => void;
	}

	let { sessionId, rows, soundcloudReady, oncopy, ondownload }: Props = $props();

	const sessionTitle = $derived(sessionId);
</script>

<section class="set-tracklist" aria-label="Tracklist">
	<h4>Tracklist</h4>
	{#if rows.length === 0}
		<p class="empty">{EMPTY_HINT}</p>
	{:else}
		<div role="group" aria-label="Copy as" class="button-row">
			{#each COPY_FORMATS as item (item.format)}
				<button
					type="button"
					disabled={item.format === 'soundcloud' && !soundcloudReady}
					title={item.format === 'soundcloud' && !soundcloudReady
						? 'Acknowledge the rights reminder in the SoundCloud tracklist panel first'
						: `Copy tracklist as ${item.label}`}
					onclick={() => oncopy(item.format)}
				>
					{item.label}
				</button>
			{/each}
		</div>
		<div role="group" aria-label="Download" class="button-row">
			{#each DOWNLOAD_FORMATS as item (item.format)}
				<button type="button" onclick={() => ondownload(item.format)}>{item.label}</button>
			{/each}
		</div>
		<table>
			<caption class="visually-hidden"
				>Tracks played in session {sessionTitle}, in play order with timings</caption
			>
			<thead>
				<tr>
					<th scope="col">#</th>
					<th scope="col">Start</th>
					<th scope="col">Artist</th>
					<th scope="col">Title</th>
					<th scope="col">Key</th>
					<th scope="col">Played</th>
					<th scope="col">Transition</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.position)}
					<tr>
						<td title="Play order position in this set">{row.position}</td>
						<td title="Minutes and seconds from the start of the set">{row.start_label}</td>
						<td>{row.artist ?? '--'}</td>
						<td>
							{row.title ?? '--'}
							{#if row.hidden}
								<small class="hidden-hint" title="This track is hidden from tracklists"
									>{HIDDEN_HINT}</small
								>
							{/if}
						</td>
						<td>{row.key ?? '--'}</td>
						<td title="How long this track played">{row.played_label ?? '--'}</td>
						<td>{row.transition_label ?? '--'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</section>

<style>
	h4 {
		margin: 0 0 0.5rem;
	}
	.empty {
		color: var(--muted);
		margin: 0;
	}
	.button-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-bottom: 0.75rem;
	}
	button {
		font-size: 0.85rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	th,
	td {
		border: 1px solid var(--border);
		padding: 0.35rem 0.5rem;
		text-align: left;
	}
	.hidden-hint {
		display: block;
		color: var(--muted);
		font-size: 0.75rem;
	}
	.visually-hidden {
		border: 0;
		clip: rect(0 0 0 0);
		height: 1px;
		margin: -1px;
		overflow: hidden;
		padding: 0;
		position: absolute;
		width: 1px;
		white-space: nowrap;
	}
</style>
