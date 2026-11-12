/**
 * The open playlist pane's aggregate summary line (SETPREP-10645).
 */

import { pushToast } from '$lib/stores.svelte';
import { fetchPlaylistSummary, type PlaylistSummaryStatsDoc } from './api-playlist-summary';

class PlaylistSummaryState {
	playlistId = $state<string | null>(null);
	stats = $state<PlaylistSummaryStatsDoc | null>(null);

	attach(playlistId: string | null): void {
		if (playlistId === this.playlistId) return;
		this.playlistId = playlistId;
		this.stats = null;
		if (playlistId !== null) void this.refresh();
	}

	async refresh(): Promise<void> {
		const id = this.playlistId;
		if (id === null) return;
		try {
			const doc = await fetchPlaylistSummary(id);
			if (this.playlistId === id) this.stats = doc;
		} catch (error) {
			pushToast(error instanceof Error ? error.message : String(error), 'error');
		}
	}
}

export const playlistSummaryState = new PlaylistSummaryState();
