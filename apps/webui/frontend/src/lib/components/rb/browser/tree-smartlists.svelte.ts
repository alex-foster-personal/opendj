/**
 * Smartlists tree-section state, split out of PlaylistTree.svelte's script
 * (self-fetched here - LANE smartlists-router - so this stays a distinct
 * concern from playlist row rendering and needs zero BrowserPanel /
 * api-rb.ts (hotspot) changes).
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state.
 */
import {
	createSmartlist,
	deleteSmartlist,
	duplicateSmartlist,
	getSmartlistWithEtag,
	listSmartlists,
	updateSmartlist,
	type SmartlistRule,
	type SmartlistSummary
} from '$lib/rb/api-smartlists';
import {
	subscribeKind,
	subscribeResync,
	type LibraryKind,
	type KindListener,
	type ResyncListener,
	type Unsubscribe
} from '$lib/api/events-bus';
import { coalesce } from '$lib/rb/coalesce';

type ListSmartlists = typeof listSmartlists;
type SubscribeKind = (kind: LibraryKind, listener: KindListener) => Unsubscribe;
type SubscribeResync = (listener: ResyncListener) => Unsubscribe;

export class TreeSmartlists {
	open = $state(true);
	rows = $state<SmartlistSummary[] | null>(null);
	error = $state<string | null>(null);
	readonly #requestReload: () => Promise<void>;
	readonly #unsubscribes: Unsubscribe[];

	constructor(
		private readonly onselect: () => ((smartlist: SmartlistSummary) => void) | undefined,
		private readonly list: ListSmartlists = listSmartlists,
		subscribeToKind: SubscribeKind = subscribeKind,
		subscribeToResync: SubscribeResync = subscribeResync
	) {
		this.#requestReload = coalesce(() => this.reload());
		this.#unsubscribes = [
			...(['tracks', 'smartlists', 'mytags', 'pairings'] as const).map((kind) =>
				subscribeToKind(kind, () => void this.#requestReload())
			),
			subscribeToResync(() => void this.#requestReload())
		];
		void this.#requestReload();
	}

	async reload(): Promise<void> {
		try {
			this.rows = await this.list({ includeCounts: true });
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

	click(sl: SmartlistSummary): void {
		const onselect = this.onselect();
		if (onselect) onselect(sl);
	}

	async create(name: string, rule: SmartlistRule): Promise<SmartlistSummary> {
		const row = await createSmartlist({ name, rule });
		await this.reload();
		return row;
	}

	async rename(id: string, name: string): Promise<void> {
		const { summary, etag } = await getSmartlistWithEtag(id);
		await updateSmartlist(id, { rule: summary.rule, name }, etag);
		await this.reload();
	}

	async duplicate(id: string): Promise<SmartlistSummary> {
		const row = await duplicateSmartlist(id);
		await this.reload();
		return row;
	}

	async remove(id: string): Promise<void> {
		await deleteSmartlist(id);
		if (this.rows !== null) {
			this.rows = this.rows.filter((row) => row.id !== id);
		}
	}

	destroy(): void {
		for (const unsubscribe of this.#unsubscribes) unsubscribe();
	}
}
