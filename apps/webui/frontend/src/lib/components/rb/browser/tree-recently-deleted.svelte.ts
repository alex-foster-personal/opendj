/**
 * Recently deleted playlists tree-section state (LIBMX-03).
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state.
 */
import {
	listDeletedPlaylists,
	undeletePlaylist,
	type DeletedPlaylistOut
} from '$lib/rb/playlist-deleted';
import {
	subscribeKind,
	subscribeResync,
	type KindListener,
	type LibraryKind,
	type ResyncListener,
	type Unsubscribe
} from '$lib/api/events-bus';
import { coalesce } from '$lib/rb/coalesce';

type ListDeletedPlaylists = typeof listDeletedPlaylists;
type UndeletePlaylist = typeof undeletePlaylist;
type SubscribeKind = (kind: LibraryKind, listener: KindListener) => Unsubscribe;
type SubscribeResync = (listener: ResyncListener) => Unsubscribe;

export class TreeRecentlyDeleted {
	open = $state(false);
	rows = $state<DeletedPlaylistOut[] | null>(null);
	error = $state<string | null>(null);
	readonly #requestReload: () => Promise<void>;
	readonly #unsubscribes: Unsubscribe[];

	constructor(
		private readonly list: ListDeletedPlaylists = listDeletedPlaylists,
		private readonly restoreFn: UndeletePlaylist = undeletePlaylist,
		subscribeToKind: SubscribeKind = subscribeKind,
		subscribeToResync: SubscribeResync = subscribeResync
	) {
		this.#requestReload = coalesce(() => this.reload());
		this.#unsubscribes = [
			subscribeToKind('playlists', () => void this.#requestReload()),
			subscribeToResync(() => void this.#requestReload())
		];
		void this.#requestReload();
	}

	async reload(): Promise<void> {
		try {
			this.rows = await this.list();
			this.error = null;
		} catch (err: unknown) {
			this.error =
				typeof err === 'object' && err !== null && 'code' in err && typeof err.code === 'string'
					? err.code
					: String(err);
		}
	}

	toggle(): void {
		this.open = !this.open;
	}

	async restore(id: string): Promise<void> {
		await this.restoreFn(id);
		if (this.rows !== null) {
			this.rows = this.rows.filter((row) => row.playlist_id !== id);
		}
	}

	destroy(): void {
		for (const unsubscribe of this.#unsubscribes) unsubscribe();
	}
}
