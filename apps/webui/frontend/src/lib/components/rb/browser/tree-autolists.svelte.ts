/**
 * Autolist browser state (issue #2066).
 */
import { fetchAutolistIndex, listAutolistBuckets } from '$lib/rb/api-autolists';
import type { AutolistBucket } from '$lib/smartlists/autolist-index';
import { staticBpmBuckets, staticRatingBuckets } from '$lib/smartlists/autolist-index';
import {
	createAutolistIndexWorker,
	type AutolistIndexWorkerHandle
} from '$lib/smartlists/autolist-index-host';
import {
	emptyAutolistSelection,
	summarizeAutolistSelection,
	type AutolistGroupId,
	type AutolistSelection
} from '$lib/smartlists/autolist-rule';

export class TreeAutolists {
	selection = $state<AutolistSelection>(emptyAutolistSelection());
	openGroups = $state<Record<AutolistGroupId, boolean>>({
		genre: false,
		rating: false,
		bpm: false
	});
	genreBuckets = $state<AutolistBucket[]>([]);
	error = $state<string | null>(null);
	private worker: AutolistIndexWorkerHandle | null = null;
	private indexStarted = false;

	constructor(
		private onSelectionChange: (selection: AutolistSelection, title: string) => void
	) {}

	startIndexWorker(): void {
		if (this.indexStarted) return;
		this.indexStarted = true;
		this.worker = createAutolistIndexWorker();
		void fetchAutolistIndex()
			.then((rows) => {
				this.worker?.build(rows);
			})
			.catch((exc) => {
				this.error = String(exc);
			});
	}

	toggleGroup(group: AutolistGroupId): void {
		this.openGroups[group] = !this.openGroups[group];
		if (this.openGroups[group] && group === 'genre') void this._loadGenreBuckets();
	}

	private async _loadGenreBuckets(): Promise<void> {
		if (this.worker?.ready) {
			try {
				this.genreBuckets = await this.worker.buckets('genre');
				return;
			} catch {
				// fall through to HTTP
			}
		}
		void listAutolistBuckets('genre')
			.then((buckets) => {
				this.genreBuckets = buckets.map((b) => {
					const bucket: AutolistBucket = { id: b.id, label: b.label };
					if (b.count !== null) bucket.count = b.count;
					return bucket;
				});
			})
			.catch((exc) => {
				this.error = String(exc);
			});
	}

	bucketsFor(group: AutolistGroupId): AutolistBucket[] {
		if (group === 'rating') return staticRatingBuckets();
		if (group === 'bpm') return staticBpmBuckets();
		return this.genreBuckets;
	}

	isSelected(group: AutolistGroupId, childId: string): boolean {
		return (this.selection[group] ?? []).includes(childId);
	}

	toggleChild(group: AutolistGroupId, childId: string): void {
		const current = [...(this.selection[group] ?? [])];
		const idx = current.indexOf(childId);
		if (idx >= 0) current.splice(idx, 1);
		else current.push(childId);
		this.selection = { ...this.selection, [group]: current };
		const title = summarizeAutolistSelection(this.selection);
		this.onSelectionChange(this.selection, title);
	}

	destroy(): void {
		this.worker?.terminate();
		this.worker = null;
	}
}
