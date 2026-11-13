<script lang="ts">
	// PLAYS-9745: play History in the tree slot, rekordbox History mimicked.
	// Stateless: the nights list, or one night's plays in play order. The
	// fetching wrapper is HistorySourcePanel.svelte.
	import type { HistoryDay } from '$lib/history/history-api';
	import {
		historyCountLabel,
		historyNightLabel,
		type HistoryPlayView
	} from '$lib/history/history-view';

	let {
		days,
		loading,
		error,
		night,
		plays,
		playsLoading,
		playsError,
		saving,
		oncollection,
		onopen,
		onback,
		onsave,
		ondaymenu,
		onplaymenu
	}: {
		days: HistoryDay[];
		loading: boolean;
		error: string | null;
		/** The open night (YYYY-MM-DD), or null for the nights list. */
		night: string | null;
		plays: HistoryPlayView[];
		playsLoading: boolean;
		playsError: string | null;
		saving: boolean;
		oncollection: () => void;
		onopen: (date: string) => void;
		onback: () => void;
		onsave: (date: string) => void;
		ondaymenu: (event: MouseEvent, day: HistoryDay) => void;
		onplaymenu: (event: MouseEvent, play: HistoryPlayView) => void;
	} = $props();
</script>

<div class="history-source" data-testid="history-panel">
	<div class="source-header">
		<button class="link-btn" onclick={oncollection}>Collection</button>
		<span class="history-mark">History</span>
	</div>
	{#if night === null}
		{#if loading && days.length === 0}
			<p class="status" role="status">Loading history...</p>
		{:else if error !== null}
			<p class="status error" role="alert">History failed to load: {error}</p>
		{:else if days.length === 0}
			<p class="status" role="status">
				No plays logged yet. Tracks you load and play show up here, one list per night.
			</p>
		{:else}
			<ul class="nights" data-testid="history-nights">
				{#each days as day (day.date)}
					<li>
						<button
							class="night"
							data-testid="history-night"
							data-date={day.date}
							title="Open the plays of the night starting {day.date}"
							onclick={() => onopen(day.date)}
							oncontextmenu={(event) => ondaymenu(event, day)}
						>
							<span class="night-name">{historyNightLabel(day.date)}</span>
							<span class="night-count">{historyCountLabel(day.count)}</span>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	{:else}
		<div class="night-header">
			<button class="link-btn" data-testid="history-back" onclick={onback}>Nights</button>
			<span class="night-name" data-testid="history-night-title">{historyNightLabel(night)}</span>
			<button
				class="save-btn"
				data-testid="history-save-playlist"
				disabled={saving || playsLoading || plays.length === 0}
				title="Save this night as a normal playlist, tracks in play order (repeats kept)"
				onclick={() => onsave(night!)}
			>
				{saving ? 'Saving...' : 'Save as playlist'}
			</button>
		</div>
		{#if playsLoading && plays.length === 0}
			<p class="status" role="status">Loading plays...</p>
		{:else if playsError !== null}
			<p class="status error" role="alert">Plays failed to load: {playsError}</p>
		{:else if plays.length === 0}
			<p class="status" role="status">No plays on this night.</p>
		{:else}
			<ol class="plays" data-testid="history-plays">
				{#each plays as play (play.position)}
					<li
						class="play"
						class:gone={play.gone}
						data-testid="history-play"
						data-stable-id={play.stable_id}
						oncontextmenu={(event) => onplaymenu(event, play)}
					>
						<span class="pos">{play.position}</span>
						<span class="time" title="Played at">{play.time}</span>
						<span class="title">{play.title}</span>
						<span class="artist">{play.artist}</span>
					</li>
				{/each}
			</ol>
		{/if}
	{/if}
</div>

<style>
	.history-source {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
		background: var(--rb-panel);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
	}
	.source-header,
	.night-header {
		display: flex;
		align-items: center;
		gap: 6px;
		min-height: 24px;
		padding: 0 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.history-mark {
		font-weight: 600;
	}
	.link-btn {
		border: 0;
		padding: 0;
		background: transparent;
		color: var(--rb-accent);
		font: inherit;
		cursor: pointer;
	}
	.save-btn {
		margin-left: auto;
		border: 1px solid var(--rb-border);
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	.save-btn:disabled {
		color: var(--rb-text-dim);
		cursor: default;
	}
	.status {
		margin: 0;
		padding: 6px;
		color: var(--rb-text-dim);
	}
	.status.error {
		color: var(--rb-danger, #d0342c);
	}
	.nights,
	.plays {
		flex: 1;
		min-height: 0;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		list-style: none;
	}
	.night {
		display: flex;
		justify-content: space-between;
		width: 100%;
		height: 22px;
		padding: 0 6px;
		border: 0;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.night:hover {
		background: var(--rb-panel-raised);
	}
	.night-count,
	.pos,
	.time,
	.artist {
		color: var(--rb-text-dim);
	}
	.play {
		display: grid;
		grid-template-columns: 2.2em 3.2em minmax(0, 1fr);
		grid-template-rows: auto auto;
		column-gap: 4px;
		padding: 2px 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.play .title,
	.play .artist {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.play .artist {
		grid-column: 3;
	}
	.play.gone .title {
		font-style: italic;
		color: var(--rb-text-dim);
	}
</style>
