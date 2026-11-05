/**
 * The open playlist's set target, skip-in-set flags and summary (LIBUX-8679).
 *
 * BrowserPanel attaches the editable pane's playlist id; the header readout,
 * the row dimming (TrackTable `rb-row-set-skip`) and the "Skip in set" menu
 * item and shortcut all read and write through this one store. The server
 * document is the truth: every write replaces it with the route's answer.
 */

import { pushToast } from '$lib/stores.svelte';
import { fetchSetSummary, putSetSkip, putSetTarget, type SetSummaryDoc } from './api-set-target';

/** Cmd/Ctrl+Shift+X on a focused row toggles "Skip in set" (shown in the menu item label). */
export const SET_SKIP_SHORTCUT = { key: 'x', label: 'Cmd/Ctrl+Shift+X' } as const;

export function isSetSkipShortcut(e: {
	key: string;
	shiftKey: boolean;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
}): boolean {
	return (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === SET_SKIP_SHORTCUT.key;
}

class SetTargetState {
	playlistId = $state<string | null>(null);
	summary = $state<SetSummaryDoc | null>(null);
	skipIds = $derived(new Set(this.summary?.skipped_ids ?? []));

	attach(playlistId: string | null): void {
		if (playlistId === this.playlistId) return;
		this.playlistId = playlistId;
		this.summary = null;
		if (playlistId !== null) void this.refresh();
	}

	async refresh(): Promise<void> {
		const id = this.playlistId;
		if (id === null) return;
		await this.apply(id, () => fetchSetSummary(id));
	}

	async setTarget(targetS: number | null): Promise<void> {
		const id = this.playlistId;
		if (id === null) return;
		await this.apply(id, () => putSetTarget(id, targetS));
	}

	/** Toggle each id; all follow the first id's new state, like a check item. */
	async toggleSkip(stableIds: readonly string[]): Promise<void> {
		const id = this.playlistId;
		if (id === null || stableIds.length === 0) return;
		const skipped = !this.skipIds.has(stableIds[0]);
		for (const sid of stableIds) await this.apply(id, () => putSetSkip(id, sid, skipped));
	}

	private async apply(id: string, call: () => Promise<SetSummaryDoc>): Promise<void> {
		try {
			const doc = await call();
			if (this.playlistId === id) this.summary = doc;
		} catch (error) {
			pushToast(error instanceof Error ? error.message : String(error), 'error');
		}
	}
}

export const setTargetState = new SetTargetState();
