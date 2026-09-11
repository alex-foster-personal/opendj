/**
 * Host helper for the autolist index worker (issue #2066).
 */
import type { AutolistBucket, AutolistIndexRow } from './autolist-index';
import type { AutolistGroupId, AutolistSelection } from './autolist-rule';

export interface AutolistIndexWorkerHandle {
	build(rows: AutolistIndexRow[]): void;
	query(selection: AutolistSelection): Promise<string[]>;
	buckets(group: AutolistGroupId): Promise<AutolistBucket[]>;
	ready: boolean;
	terminate(): void;
}

export function createAutolistIndexWorker(): AutolistIndexWorkerHandle {
	const worker = new Worker(new URL('./autolist-index.worker.ts', import.meta.url), {
		type: 'module'
	});
	let ready = false;
	const pendingQueries = new Map<number, (ids: string[]) => void>();
	const pendingBuckets = new Map<number, (buckets: AutolistBucket[]) => void>();
	let querySeq = 0;
	let bucketSeq = 0;

	worker.onmessage = (event: MessageEvent) => {
		const msg = event.data as {
			type: string;
			count?: number;
			ids?: string[];
			buckets?: AutolistBucket[];
			seq?: number;
		};
		if (msg.type === 'built') {
			ready = true;
			return;
		}
		if (msg.type === 'query-result' && msg.seq !== undefined) {
			const resolve = pendingQueries.get(msg.seq);
			if (resolve !== undefined) {
				pendingQueries.delete(msg.seq);
				resolve(msg.ids ?? []);
			}
			return;
		}
		if (msg.type === 'buckets' && msg.seq !== undefined) {
			const resolve = pendingBuckets.get(msg.seq);
			if (resolve !== undefined) {
				pendingBuckets.delete(msg.seq);
				resolve(msg.buckets ?? []);
			}
		}
	};

	return {
		get ready() {
			return ready;
		},
		build(rows: AutolistIndexRow[]) {
			worker.postMessage({ type: 'build', rows });
		},
		query(selection: AutolistSelection): Promise<string[]> {
			const seq = ++querySeq;
			return new Promise((resolve) => {
				pendingQueries.set(seq, resolve);
				worker.postMessage({ type: 'query', selection, seq });
			});
		},
		buckets(group: AutolistGroupId): Promise<AutolistBucket[]> {
			const seq = ++bucketSeq;
			return new Promise((resolve) => {
				pendingBuckets.set(seq, resolve);
				worker.postMessage({ type: 'buckets', group, seq });
			});
		},
		terminate() {
			worker.terminate();
		}
	};
}
