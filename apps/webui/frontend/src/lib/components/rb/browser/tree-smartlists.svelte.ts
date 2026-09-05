/**
 * Smartlists tree-section state, split out of PlaylistTree.svelte's script
 * (self-fetched here - LANE smartlists-router - so this stays a distinct
 * concern from playlist row rendering and needs zero BrowserPanel /
 * api-rb.ts (hotspot) changes).
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state.
 */
import { listSmartlists, type SmartlistSummary } from '$lib/rb/api-smartlists';
import { RbApiError } from '$lib/rb/api-rb';

export class TreeSmartlists {
	open = $state(true);
	rows = $state<SmartlistSummary[] | null>(null);
	error = $state<string | null>(null);

	constructor(private readonly onselect: () => ((smartlist: SmartlistSummary) => void) | undefined) {
		listSmartlists().then(
			(rows) => (this.rows = rows),
			(err: unknown) => {
				// Explicit backend error (e.g. SMARTLISTS_DB_UNAVAILABLE on an
				// in-memory deploy) renders as a dim error row - never hidden.
				this.error = err instanceof RbApiError ? err.code : String(err);
			}
		);
	}

	toggle(): void {
		this.open = !this.open;
	}

	click(sl: SmartlistSummary): void {
		const onselect = this.onselect();
		if (onselect) onselect(sl);
	}
}
