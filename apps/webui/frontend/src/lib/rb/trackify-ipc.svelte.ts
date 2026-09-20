/**
 * Browser IPC for Trackify listening mode (agent parity, PERFMODE-15).
 */
import {
	e2eForceTrackifyLoad,
	readTrackifyAutoplayState,
	requestTrackifySkipNext
} from '$lib/rb/trackify-autoplay.svelte';
import { e2ePrimeTrackifyFeed, readTrackifyFeedSnapshot } from '$lib/rb/trackify-feed.svelte';
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';

interface TrackifyBrowserIpc {
	version: 1;
	query(): ReturnType<typeof readTrackifyAutoplayState> & { feed_epoch: number; feed_scope: string };
	skip_next(): ReturnType<typeof readTrackifyAutoplayState>;
	toggle_autoplay(enabled: unknown): ReturnType<typeof readTrackifyAutoplayState>;
	e2e_prime_feed?(rows: readonly AutoPlayTrackRow[]): ReturnType<typeof readTrackifyAutoplayState>;
	e2e_force_load?(stableId: string): Promise<ReturnType<typeof readTrackifyAutoplayState>>;
}

declare global {
	interface Window {
		musicDjToolsTrackify?: TrackifyBrowserIpc;
	}
}

function _query() {
	const feed = readTrackifyFeedSnapshot();
	return Object.freeze({
		...readTrackifyAutoplayState(),
		feed_epoch: feed.epoch,
		feed_scope: feed.scope
	});
}

export function installTrackifyBrowserIpc(): () => void {
	if (typeof window === 'undefined') throw new Error('Trackify IPC requires a browser window');
	if (window.musicDjToolsTrackify !== undefined) throw new Error('Trackify IPC is already installed');
	const ipc: TrackifyBrowserIpc = Object.freeze({
		version: 1 as const,
		query: () => _query(),
		skip_next: () => {
			requestTrackifySkipNext();
			return _query();
		},
		toggle_autoplay: (enabled: unknown) => {
			if (typeof enabled !== 'boolean') {
				throw new TypeError('toggle_autoplay expects a boolean');
			}
			setAutoPlayEnabled(enabled);
			return _query();
		},
		...(import.meta.env.DEV
			? {
					e2e_prime_feed: (rows: readonly AutoPlayTrackRow[]) => {
						e2ePrimeTrackifyFeed(rows);
						return _query();
					},
					e2e_force_load: async (stableId: string) => {
						if (typeof stableId !== 'string' || stableId === '') {
							throw new TypeError('e2e_force_load expects a non-empty stable_id');
						}
						await e2eForceTrackifyLoad(stableId);
						return _query();
					}
				}
			: {})
	});
	window.musicDjToolsTrackify = ipc;
	return () => {
		if (window.musicDjToolsTrackify !== ipc) {
			throw new Error('Trackify IPC ownership changed before cleanup');
		}
		delete window.musicDjToolsTrackify;
	};
}

export function readTrackifyAutoplayEnabled(): boolean {
	return uiPrefs.auto_play_enabled;
}
